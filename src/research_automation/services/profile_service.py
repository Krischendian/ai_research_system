"""业务画像：基于公开文件节选 + LLM 抽取（禁止臆测与投资建议）。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from research_automation.extractors.llm_client import chat
from research_automation.models.company import BusinessProfile

# POC：尚未接入真实 10-K，以下为与某消费电子公司结构类似的【虚构节选示例】，仅用于联调 Prompt 与 JSON 解析。
_SAMPLE_FILING_EXCERPT = """
Item 1. Business

The Company designs, manufactures and markets mobile communication and media devices,
personal computers, and portable digital music players, and sells a variety of related
software, services, accessories, networking solutions, and third-party digital content and
applications.

Products and services include smartphone hardware, personal computers, tablets, wearables
and accessories, and subscription-based Internet services including cloud and digital
distribution platforms.

For the fiscal year ended September 30, 2023, net sales by reportable segment were:
iPhone 52% of total net sales; Mac 10%; iPad 8%; Wearables, Home and Accessories 11%;
and Services 19%.

For the same period, net sales by region were: Americas 42%; Europe 24%;
Greater China 18%; Japan 7%; and Rest of Asia Pacific 9%. These percentages are
stated as approximate shares of total net sales as disclosed in management discussion.
""".strip()


class ProfileGenerationError(Exception):
    """无法生成业务画像（可向 API 客户端返回友好说明）。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _build_prompt(symbol: str, excerpt: str) -> str:
    return f"""你是一名严谨的财务文件摘录助手。根据下面【文件节选】中的事实性内容完成结构化输出。

硬性要求：
1. 仅使用【文件节选】中明确出现或可直接概括的信息；不得引入节选之外的公司、数据或事件。
2. 禁止输出投资建议、估值观点、风险评级、业绩预测、「应该买入/卖出」等任何主观判断或建议。
3. 用词客观、简短，core_business 为对主营业务的中性描述（可为中文）。
4. revenue_by_segment、revenue_by_geography 中的 percentage 必须是字符串，且必须包含字符「%」，例如 \"52.0%\"；数值须与节选一致，不得编造节选未给出的占比。
5. 仅输出一个 JSON 对象，键名必须为：core_business, revenue_by_segment, revenue_by_geography。其中两个 revenue 数组的元素为对象，含 segment_name（字符串）与 percentage（字符串，带%）。

用户请求的证券代码（仅供输出校验，不要在描述中虚构该公司未在节选出现的信息）：{symbol}

【文件节选】
{excerpt}
"""


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON 根节点须为对象")
    return data


def _normalize_percentage(value: str) -> str:
    """确保占比字符串含「%」（若模型漏写则补全）。"""
    s = (value or "").strip()
    if not s:
        return "0%"
    if "%" not in s:
        return f"{s}%"
    return s


def _normalize_mix_lists(data: dict[str, Any]) -> None:
    for key in ("revenue_by_segment", "revenue_by_geography"):
        items = data.get(key)
        if not isinstance(items, list):
            data[key] = []
            continue
        fixed: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("segment_name")
            pct = it.get("percentage")
            if name is None or pct is None:
                continue
            fixed.append(
                {
                    "segment_name": str(name).strip(),
                    "percentage": _normalize_percentage(str(pct)),
                }
            )
        data[key] = fixed


def get_profile(ticker: str) -> BusinessProfile:
    """
    基于示例节选调用 LLM，生成符合 ``BusinessProfile`` 的业务画像。
    失败时抛出 ``ProfileGenerationError``（带可读中文说明）。
    """
    symbol = (ticker or "").strip().upper() or "UNKNOWN"

    prompt = _build_prompt(symbol, _SAMPLE_FILING_EXCERPT)
    try:
        reply = chat(
            prompt,
            response_format={"type": "json_object"},
            timeout=90.0,
        )
    except ValueError as e:
        raise ProfileGenerationError(f"语言模型未就绪：{e}") from e
    except RuntimeError as e:
        raise ProfileGenerationError(f"调用语言模型失败：{e}") from e

    try:
        payload = _extract_json_object(reply)
    except (json.JSONDecodeError, ValueError) as e:
        raise ProfileGenerationError(
            "模型返回内容无法解析为 JSON，请稍后重试或检查服务日志。"
        ) from e

    payload["ticker"] = symbol
    payload["last_updated"] = datetime.now(timezone.utc).isoformat()
    payload["data_source_label"] = (
        "项目内嵌「Item 1」式英文示例节选（非真实 10-K 全文）+ OpenAI API 结构化抽取；"
        "占比与表述须以 SEC EDGAR 法定披露为准。"
    )
    payload["primary_source_url"] = f"https://www.sec.gov/edgar/search/#/q={symbol}"
    _normalize_mix_lists(payload)

    try:
        return BusinessProfile.model_validate(payload)
    except Exception as e:
        raise ProfileGenerationError(
            f"业务画像字段未通过校验（占比须含 % 等）：{e}"
        ) from e
