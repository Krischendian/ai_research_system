"""OpenAI Chat 封装。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError, APITimeoutError, AuthenticationError, OpenAI, RateLimitError

# llm_client.py -> extractors -> research_automation -> src -> 项目根
_ROOT = Path(__file__).resolve().parents[3]

_PLACEHOLDER_KEYS = frozenset({"", "你的key", "your-api-key-here", "sk-placeholder"})


def _ensure_env() -> None:
    load_dotenv(_ROOT / ".env")


def chat(
    prompt: str,
    *,
    model: str = "gpt-4o-mini",
    timeout: float = 60.0,
    response_format: dict[str, str] | None = None,
) -> str:
    """
    调用 OpenAI Chat Completions，返回助手回复文本。

    ``response_format`` 可传 ``{"type": "json_object"}`` 以尽量保证返回合法 JSON。

    - 未配置或仍为占位符的 `OPENAI_API_KEY`：抛出 ``ValueError``
    - 认证、限流、超时及其他 API 错误：抛出 ``RuntimeError``（链式保留原异常）
    """
    _ensure_env()
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key or key in _PLACEHOLDER_KEYS:
        raise ValueError(
            "未配置有效的 OPENAI_API_KEY：请在项目根目录 .env 中设为真实密钥（不要使用占位符）。"
        )

    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt 不能为空")

    client = OpenAI(api_key=key, timeout=timeout, max_retries=0)
    try:
        kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": text}],
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        resp = client.chat.completions.create(**kwargs)
    except AuthenticationError as e:
        raise RuntimeError(
            "OpenAI 认证失败，请检查 OPENAI_API_KEY 是否正确、是否过期。"
        ) from e
    except RateLimitError as e:
        raise RuntimeError("OpenAI 请求触发频率限制，请稍后重试。") from e
    except APITimeoutError as e:
        raise RuntimeError(f"OpenAI 请求超时（超过 {timeout} 秒）。") from e
    except APIError as e:
        raise RuntimeError(f"OpenAI API 返回错误: {e}") from e

    msg = resp.choices[0].message
    content = msg.content if msg else None
    return (content or "").strip()
