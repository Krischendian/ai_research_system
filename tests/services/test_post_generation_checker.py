import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research_automation.services.post_generation_checker import (
    annotate_paragraph,
    compare_with_baseline,
    normalize_to_billion,
    run_post_generation_check,
)


BASELINE = {
    "ZM": {
        2026: {
            "revenue": 4.87,
            "net_income": 0.488,
            "gross_margin": 0.770,
            "ebitda": 0.94,
            "capex": 0.06,
        }
    },
    "TGT": {
        2025: {
            "revenue": 104.78,
            "net_income": 3.71,
            "gross_margin": 0.279,
        }
    },
}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("$1.23B", 1.23),
        ("$456M", 0.456),
        ("211亿美元", 21.1),
        ("104.4亿", pytest.approx(10.44)),
        ("7.0%", 0.07),
        ("+12.4%", 0.124),
        ("-3%", -0.03),
        ("$2.46B", 2.46),
        ("$0B", 0.0),
        ("0%", 0.0),
        ("0.001亿", 0.0001),
        ("约若干亿", None),
        ("N/A", None),
        ("", None),
        ("—", None),
    ],
    ids=[
        "dollar_b_123",
        "dollar_m_456",
        "cn_yi_usd_211",
        "cn_yi_104_4",
        "percent_7_0",
        "percent_plus_12_4",
        "percent_minus_3",
        "dollar_b_246",
        "zero_b",
        "zero_percent",
        "small_yi",
        "unparseable_cn_text",
        "unparseable_na",
        "unparseable_empty",
        "unparseable_dash",
    ],
)
def test_normalize_to_billion(raw, expected):
    assert normalize_to_billion(raw) == expected


@pytest.mark.parametrize(
    "attributed",
    [
        {"raw": "$4.87B", "ticker": "ZM", "metric": "revenue"},
        {"raw": "$4.85B", "ticker": "ZM", "metric": "revenue"},
        {"raw": "77.0%", "ticker": "ZM", "metric": "gross_margin"},
    ],
    ids=[
        "revenue_exact_match",
        "revenue_within_tolerance",
        "gross_margin_percent_exact_match",
    ],
)
def test_compare_with_baseline_pass(attributed):
    result = compare_with_baseline(attributed, BASELINE)
    assert result["status"] == "✅"


@pytest.mark.parametrize(
    "attributed",
    [
        {"raw": "$4.5B", "ticker": "ZM", "metric": "revenue"},
    ],
    ids=["revenue_between_2_and_10_percent"],
)
def test_compare_with_baseline_warning(attributed):
    result = compare_with_baseline(attributed, BASELINE)
    assert result["status"] == "⚠️"


@pytest.mark.parametrize(
    "attributed",
    [
        {"raw": "$1.90B", "ticker": "ZM", "metric": "net_income"},
        {"raw": "60.0%", "ticker": "ZM", "metric": "gross_margin"},
    ],
    ids=[
        "net_income_over_10_percent",
        "gross_margin_over_10_percent",
    ],
)
def test_compare_with_baseline_error(attributed):
    result = compare_with_baseline(attributed, BASELINE)
    assert result["status"] == "🔴"


@pytest.mark.parametrize(
    "attributed",
    [
        {"raw": "$4.87B", "ticker": None, "metric": "revenue"},
        {"raw": "$4.87B", "ticker": "ZM", "metric": None},
        {"raw": "7.0%", "ticker": "ZM", "metric": "yoy_growth"},
        {"raw": "若干亿", "ticker": "ZM", "metric": "revenue"},
    ],
    ids=[
        "missing_ticker",
        "missing_metric",
        "metric_not_in_baseline",
        "raw_unparseable",
    ],
)
def test_compare_with_baseline_unverified(attributed):
    result = compare_with_baseline(attributed, BASELINE)
    assert result["status"] == "🔵"


def test_compare_with_baseline_unverified_when_baseline_is_zero():
    baseline_with_zero = {
        "ZM": {
            2026: {
                "revenue": 0.0,
            }
        }
    }
    attributed = {"raw": "$4.87B", "ticker": "ZM", "metric": "revenue"}
    result = compare_with_baseline(attributed, baseline_with_zero)
    assert result["status"] == "🔵"
    assert result["reason"] == "基线值缺失或为零，无法核实"


def test_compare_with_baseline_unverified_yoy_growth_reason():
    attributed = {"raw": "7.0%", "ticker": "ZM", "metric": "yoy_growth"}
    result = compare_with_baseline(attributed, BASELINE)
    assert result["status"] == "🔵"
    assert result["reason"] == "同比增速无年报基线可比对"


@pytest.mark.parametrize(
    "paragraph, expected_status",
    [
        ("Zoom本财年营收为$4.87B。", "✅"),
        ("Zoom本财年净利润为$1.90B，创历史新高。", "🔴"),
        ("本板块整体收入约$300B，同比增长5%。", "🔵"),
    ],
    ids=["correct_number", "wrong_number", "unattributable"],
)
def test_annotate_paragraph_markup(paragraph, expected_status):
    result, findings = annotate_paragraph(paragraph, BASELINE)
    assert result == paragraph
    assert any(f["status"] == expected_status for f in findings)


@pytest.mark.parametrize(
    "paragraph, expected_text, expected_findings_len",
    [
        ("", "", 0),
        ("本季度管理层对宏观环境保持审慎态度。", "本季度管理层对宏观环境保持审慎态度。", 0),
    ],
    ids=["empty_string", "no_numbers"],
)
def test_annotate_paragraph_edge(paragraph, expected_text, expected_findings_len):
    result, findings = annotate_paragraph(paragraph, BASELINE)
    assert result == expected_text
    assert len(findings) == expected_findings_len


def test_run_post_generation_check_summary_counts_and_annotations():
    texts = {
        "executive_summary": "Zoom净利润为$1.90B。",
        "step4_sector_summary": "Zoom营收$4.87B。",
        "step4_companies": {},
    }
    result = run_post_generation_check(texts, BASELINE)
    assert result["summary"]["error"] >= 1
    assert result["summary"]["unverified"] >= 1
    assert "executive_summary" in result["annotated"]
    assert "step4_sector_summary" in result["annotated"]


def test_run_post_generation_check_step4_quarterly_mode_marks_blue():
    texts = {
        "step4_sector_summary": "Zoom本季收入$1.00B。",
        "step4_companies": {"ZM": "Zoom单季营收$1.10B。"},
    }
    result = run_post_generation_check(texts, BASELINE)
    assert result["summary"]["error"] == 0
    assert result["summary"]["warning"] == 0
    assert result["summary"]["unverified"] >= 2
    assert result["annotated"]["step4_sector_summary"] == texts["step4_sector_summary"]
    assert result["annotated"]["step4_companies"]["ZM"] == texts["step4_companies"]["ZM"]
