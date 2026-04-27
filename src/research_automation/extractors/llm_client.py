"""LLM 封装：优先 Claude（Anthropic），回退 OpenAI。"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]
_PLACEHOLDER_KEYS = frozenset({"", "你的key", "your-api-key-here", "sk-placeholder"})


def _ensure_env() -> None:
    load_dotenv(_ROOT / ".env")


def chat(
    prompt: str,
    *,
    model: str | None = None,
    timeout: float = 60.0,
    max_tokens: int | None = None,
    response_format: dict[str, str] | None = None,
) -> str:
    """
    调用 LLM，返回助手回复文本。优先使用 Claude（Anthropic），回退 OpenAI。

    ``max_tokens`` 可选，用于限制输出长度（Claude 默认 8096；OpenAI 未传则不设）。
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
            max_tokens=max_tokens,
            response_format=response_format,
        )

    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if openai_key and openai_key not in _PLACEHOLDER_KEYS:
        return _chat_openai(
            prompt,
            api_key=openai_key,
            model=model or "gpt-4o-mini",
            timeout=timeout,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    raise ValueError(
        "未配置有效的 LLM API Key：请在 .env 中设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY。"
    )


def _is_retryable_llm_transport_error(exc: BaseException) -> bool:
    """连接/传输类失败：可对 Claude 或 OpenAI 路径做有限次退避重试。"""
    try:
        import anthropic

        if isinstance(exc, anthropic.APIConnectionError):
            return True
    except ImportError:
        pass

    _ename = type(exc).__name__
    if any(
        x in _ename
        for x in (
            "RemoteProtocolError",
            "ConnectError",
            "ReadTimeout",
            "WriteTimeout",
            "PoolTimeout",
        )
    ):
        return True

    if isinstance(exc, RuntimeError):
        msg = str(exc)
        low = msg.lower()
        cause = exc.__cause__
        try:
            import anthropic

            if isinstance(cause, anthropic.APIConnectionError):
                return True
        except ImportError:
            pass
        if "Claude API 调用失败" in msg:
            if cause is not None and _is_retryable_llm_transport_error(cause):
                return True
            return any(
                x in low
                for x in (
                    "connection error",
                    "apiconnectionerror",
                    "remoteprotocol",
                    "disconnected",
                    "server disconnected",
                    "timeout",
                    "timed out",
                    "read timeout",
                    "connecterror",
                    "broken pipe",
                    "connection reset",
                )
            )
        if "OpenAI 请求超时" in msg:
            return True
        if "OpenAI API 返回错误" in msg and "timeout" in low:
            return True
    return False


_RETRY_BACKOFF_SEC = (10, 30, 60)


def chat_with_retry(
    prompt: str,
    *,
    max_retries: int = 5,
    **kwargs: Any,
) -> str:
    """
    对 ``chat`` 做有限次重试（默认最多 5 次调用），主要针对连接类错误。
    相邻重试间隔为 10s / 30s / 60s；若仍需继续等待，之后固定为 60s。
    其余参数与 :func:`chat` 相同（如 ``timeout``、``max_tokens``、``response_format``）。
    """
    last: BaseException | None = None
    for attempt in range(max_retries):
        try:
            return chat(prompt, **kwargs)
        except Exception as e:
            last = e
            if attempt >= max_retries - 1 or not _is_retryable_llm_transport_error(e):
                raise
            wait = (
                _RETRY_BACKOFF_SEC[attempt]
                if attempt < len(_RETRY_BACKOFF_SEC)
                else _RETRY_BACKOFF_SEC[-1]
            )
            logger.warning(
                "LLM 连接/传输失败，第 %s/%s 次重试，等待 %ss：%s",
                attempt + 1,
                max_retries,
                wait,
                e,
            )
            time.sleep(wait)
    assert last is not None
    raise last


def _chat_claude(
    prompt: str,
    *,
    api_key: str,
    model: str,
    timeout: float,
    max_tokens: int | None,
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
            max_tokens=max_tokens if max_tokens is not None else 8096,
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
    max_tokens: int | None,
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
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
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
