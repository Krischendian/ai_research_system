"""LLM 封装：优先 Claude（Anthropic），回退 OpenAI。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[3]
_PLACEHOLDER_KEYS = frozenset({"", "你的key", "your-api-key-here", "sk-placeholder"})


def _ensure_env() -> None:
    load_dotenv(_ROOT / ".env")


def chat(
    prompt: str,
    *,
    model: str | None = None,
    timeout: float = 60.0,
    response_format: dict[str, str] | None = None,
) -> str:
    """
    调用 LLM，返回助手回复文本。优先使用 Claude（Anthropic），回退 OpenAI。

    ``response_format`` 传 ``{"type": "json_object"}`` 时强制返回 JSON。
    - 未配置有效密钥：抛出 ``ValueError``
    - API 错误：抛出 ``RuntimeError``
    """
    _ensure_env()

    anthropic_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if anthropic_key and anthropic_key not in _PLACEHOLDER_KEYS:
        return _chat_claude(
            prompt,
            api_key=anthropic_key,
            model=model or os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-4-6",
            timeout=timeout,
            response_format=response_format,
        )

    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if openai_key and openai_key not in _PLACEHOLDER_KEYS:
        return _chat_openai(
            prompt,
            api_key=openai_key,
            model=model or "gpt-4o-mini",
            timeout=timeout,
            response_format=response_format,
        )

    raise ValueError(
        "未配置有效的 LLM API Key：请在 .env 中设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY。"
    )


def _chat_claude(
    prompt: str,
    *,
    api_key: str,
    model: str,
    timeout: float,
    response_format: dict[str, str] | None,
) -> str:
    """调用 Anthropic Claude API。"""
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "缺少 anthropic 库，请运行：pip install anthropic"
        ) from e

    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt 不能为空")

    # JSON 模式：在 prompt 末尾加明确指令
    if response_format and response_format.get("type") == "json_object":
        text = text + "\n\n请只输出一个合法的 JSON 对象，不要包含任何 Markdown 代码块或额外说明。"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=8096,
            messages=[{"role": "user", "content": text}],
            timeout=max(timeout, 180.0),
        )
        parts: list[str] = []
        for block in resp.content or []:
            t = getattr(block, "text", None)
            if isinstance(t, str) and t:
                parts.append(t)
        return "".join(parts).strip()
    except Exception as e:
        raise RuntimeError(f"Claude API 调用失败: {e}") from e


def _chat_openai(
    prompt: str,
    *,
    api_key: str,
    model: str,
    timeout: float,
    response_format: dict[str, str] | None,
) -> str:
    """调用 OpenAI API（回退）。"""
    try:
        from openai import APIError, APITimeoutError, AuthenticationError, OpenAI, RateLimitError
    except ImportError as e:
        raise RuntimeError("缺少 openai 库，请运行：pip install openai") from e

    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt 不能为空")

    client = OpenAI(api_key=api_key, timeout=timeout, max_retries=0)
    try:
        kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": text}],
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        resp = client.chat.completions.create(**kwargs)
    except AuthenticationError as e:
        raise RuntimeError("OpenAI 认证失败，请检查 OPENAI_API_KEY。") from e
    except RateLimitError as e:
        raise RuntimeError("OpenAI 请求触发频率限制，请稍后重试。") from e
    except APITimeoutError as e:
        raise RuntimeError(f"OpenAI 请求超时（超过 {timeout} 秒）。") from e
    except APIError as e:
        raise RuntimeError(f"OpenAI API 返回错误: {e}") from e

    msg = resp.choices[0].message
    return (msg.content or "").strip()
