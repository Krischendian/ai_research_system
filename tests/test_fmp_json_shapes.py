"""FMP 客户端对 JSON 形态的容错：dict 仅在有 Error Message 时判失败；合法单对象应保留。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


@patch("research_automation.extractors.fmp_client._api_key", return_value="test-key")
@patch("research_automation.extractors.fmp_client.requests.get")
def test_fetch_statement_single_row_dict_not_treated_as_empty(
    mock_get: MagicMock,
    _ak: str,
) -> None:
    from research_automation.extractors.fmp_client import _fetch_statement

    mock_get.return_value.raise_for_status = MagicMock()
    mock_get.return_value.json.return_value = {
        "calendarYear": "2023",
        "revenue": 1_000_000_000.0,
        "netIncome": 100_000_000.0,
    }
    rows = _fetch_statement("income-statement", "AAPL", 3)
    assert len(rows) == 1
    assert rows[0].get("calendarYear") == "2023"


@patch("research_automation.extractors.fmp_client._api_key", return_value="test-key")
@patch("research_automation.extractors.fmp_client.requests.get")
def test_fetch_statement_error_dict_returns_empty(mock_get: MagicMock, _ak: str) -> None:
    from research_automation.extractors.fmp_client import _fetch_statement

    mock_get.return_value.raise_for_status = MagicMock()
    mock_get.return_value.json.return_value = {
        "Error Message": "Limit Reach . Please upgrade your plan or visit our documentation for more details at https://site.financialmodelingprep.com/"
    }
    rows = _fetch_statement("income-statement", "AAPL", 3)
    assert rows == []


@patch("research_automation.extractors.fmp_client._api_key", return_value="test-key")
@patch("research_automation.extractors.fmp_client.requests.get")
def test_get_segment_revenue_single_fiscal_dict(mock_get: MagicMock, _ak: str) -> None:
    from research_automation.extractors.fmp_client import get_segment_revenue

    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "fiscalYear": 2023,
        "data": {"iPhone": 100.0, "Services": 50.0},
    }
    out = get_segment_revenue("AAPL", 2023)
    assert out is not None
    assert len(out) == 2
    names = {x["segment"] for x in out}
    assert names == {"iPhone", "Services"}
    assert abs(sum(x["percentage"] for x in out) - 100.0) < 0.01


@patch("research_automation.extractors.fmp_client._api_key", return_value="test-key")
@patch("research_automation.extractors.fmp_client.requests.get")
def test_get_segment_revenue_error_dict(mock_get: MagicMock, _ak: str) -> None:
    from research_automation.extractors.fmp_client import get_segment_revenue

    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {"Error Message": "Invalid API KEY."}
    assert get_segment_revenue("AAPL", 2023) is None


@patch("research_automation.extractors.fmp_client._api_key", return_value="test-key")
@patch("research_automation.extractors.fmp_client.requests.get")
def test_get_segment_revenue_unrecognized_dict(mock_get: MagicMock, _ak: str) -> None:
    from research_automation.extractors.fmp_client import get_segment_revenue

    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {"symbol": "AAPL", "note": "no segments"}
    assert get_segment_revenue("AAPL", 2023) is None
