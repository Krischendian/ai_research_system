"""Streamlit 公用：API 错误格式化与全局 Toast。"""
from __future__ import annotations

import json

import requests
import streamlit as st


def format_api_error(exc: BaseException) -> str:
    """将 ``requests`` / 网络异常格式化为可读说明。"""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        try:
            payload = exc.response.json()
            detail = payload.get("detail")
            if isinstance(detail, str):
                return f"HTTP {code}：{detail}"
            if detail is not None:
                return f"HTTP {code}：{json.dumps(detail, ensure_ascii=False)[:500]}"
        except Exception:
            pass
        text = (exc.response.text or "").strip()[:400]
        reason = getattr(exc.response, "reason", None) or ""
        return f"HTTP {code}：{text or reason}"
    return f"{type(exc).__name__}：{exc}"


def queue_global_error_toast(message: str) -> None:
    """
    将错误记入 session，供根目录 ``app.py`` 在 ``pg.run()`` 后统一 ``st.toast``。
    同时适合与 ``st.error`` 并用。
    """
    st.session_state["_global_toast_error"] = (message or "")[:900]


def toast_error(message: str) -> None:
    """立即弹出红色 Toast（当前页可见）。"""
    try:
        st.toast((message or "")[:500], icon="❌")
    except Exception:
        pass


def notify_api_failure(exc: BaseException, *, prefix: str = "") -> str:
    """格式化错误、排队全局 Toast，并返回用于 ``st.error`` 的完整文案。"""
    msg = format_api_error(exc)
    full = f"{prefix}{msg}" if prefix else msg
    queue_global_error_toast(full)
    toast_error(full)
    return full
