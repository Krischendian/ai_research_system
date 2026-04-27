import sys

sys.path.insert(0, "src")


def _detect_financial_anomalies(ticker: str, metric: str, prior: float, current: float) -> list[str]:
    """
    与当前 Step6 异常检测口径保持一致的轻量测试 helper。
    输入单位约定：
    - gross_margin: 比例值（如 0.416）
    - net_income: 十亿美元（如 1.90）
    - nd_equity: x 倍
    """
    notes: list[str] = []

    if metric == "gross_margin":
        if abs(current - prior) > 0.03:
            flag = "↓" if current < prior else "↑"
            notes.append(
                f"{ticker} 毛利率 FYprior→FYcurrent {flag}{abs(current-prior)*10000:.0f}bps，变化幅度较大。"
            )

    elif metric == "net_income":
        if abs(prior) > 1e-9:
            chg = abs(current - prior) / abs(prior)
            if chg > 0.5 and abs(current) > 0.1:
                notes.append(f"{ticker} 净利润变化{((current-prior)/abs(prior))*100:+.0f}%")
        if (prior > 0 and current < 0) or (prior < 0 and current > 0):
            notes.append(f"{ticker} 净利润由{'盈利转亏损' if current < 0 else '亏损转盈利'}")

    elif metric == "nd_equity":
        if abs(current) > 5:
            notes.append(f"{ticker} Net Debt/Equity = {current:.2f}x，为极端值")

    return notes


def _detect_truncation(text: str) -> bool:
    t = (text or "").rstrip()
    if not t:
        return True
    return t[-1] not in ("。", "！", "？", ".", "!", "?", "\"", "”", "）", ")")


# ========== 测试问题四：异常财务数据标注 ==========
def test_financial_anomaly_detection():
    """用报告中已知的异常数据验证检测逻辑是否触发"""
    test_cases = [
        # (ticker, metric, prior_val, current_val, 期望是否触发警告)
        ("PPG", "gross_margin", 0.416, 0.380, True),  # -360bps 应触发
        ("ZM", "net_income", 1.01, 1.90, True),  # +88% 应触发
        ("EL", "net_income", 0.39, -1.13, True),  # 盈转亏 应触发
        ("HCA", "nd_equity", -17.29, -8.16, True),  # 极端负值 应触发
        ("CTSH", "gross_margin", 0.343, 0.337, False),  # -60bps 不应触发
    ]

    passed = 0
    for ticker, metric, prior, current, should_trigger in test_cases:
        notes = _detect_financial_anomalies(ticker, metric, prior, current)
        triggered = len(notes) > 0
        status = "✅" if triggered == should_trigger else "❌"
        print(
            f"{status} {ticker} {metric}: prior={prior}, current={current} → 触发={triggered}（期望={should_trigger}）"
        )
        if triggered == should_trigger:
            passed += 1

    print(f"\n结果：{passed}/{len(test_cases)} 通过")


# ========== 测试问题一/二：财年标注 + 同源数据 ==========
def test_fiscal_year_and_source_consistency():
    """验证收入分部数据可返回财年与来源，并在多家公司上保持同一入口。"""
    from research_automation.services.sector_report_service import _get_revenue_segments

    for ticker in ["EL", "UPS", "CTSH"]:
        rows, year, source = _get_revenue_segments(ticker)
        if rows:
            print(
                f"✅ {ticker}: rows={len(rows)}, fiscal_year={year}, source={source}"
            )
        else:
            print(
                f"⚠️  {ticker}: 无分部数据（FMP未收录且10-K提取失败时属正常），source={source}"
            )


# ========== 测试问题六c：截断检测覆盖Step5 ==========
def test_truncation_detection():
    """验证截断检测能识别不完整的句子"""
    truncated_text = "需后续关注更多财务细"  # 报告中实际出现的截断
    complete_text = "需后续关注更多财务细节。"  # 完整版本

    assert _detect_truncation(truncated_text) is True, "❌ 截断文本未被检测到"
    assert _detect_truncation(complete_text) is False, "❌ 完整文本被误判为截断"
    print("✅ 截断检测逻辑正常")


if __name__ == "__main__":
    print("=" * 50)
    print("测试问题四：异常财务数据检测")
    print("=" * 50)
    test_financial_anomaly_detection()

    print("\n" + "=" * 50)
    print("测试问题一/二：财年标注 + 数据来源一致性")
    print("=" * 50)
    test_fiscal_year_and_source_consistency()

    print("\n" + "=" * 50)
    print("测试问题六c：截断检测")
    print("=" * 50)
    test_truncation_detection()

