"""唯一把 httpx 异常 / 上游状态码 / 上游错误体映射为 GatewayError 的地方。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping

import httpx

from gateway.core.errors import GatewayError, sanitize


def _extract_message(raw: bytes | None) -> str | None:
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(data.get("message"), str):
        return data["message"]
    if isinstance(error, str):
        return error
    return None


def _retry_after_from(headers: Mapping[str, str] | None, now: float) -> float | None:
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, target.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return None


def map_status(
    status: int,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    *,
    now: float | None = None,
) -> GatewayError:
    if status == 429:
        code = "upstream_rate_limited"
    elif status in (500, 502, 504):
        code = "upstream_server_error"
    elif status in (503, 529):
        code = "upstream_overloaded"
    elif status in (401, 403):
        code = "upstream_auth_error"
    elif status == 402:
        code = "upstream_insufficient_balance"
    elif 400 <= status < 500:
        code = "upstream_bad_request"
    else:
        code = "upstream_server_error"

    upstream_message = _extract_message(body)
    details: dict[str, object] = {"upstream_status": status}
    if upstream_message:
        details["upstream_message"] = sanitize(upstream_message)
    retry_after = _retry_after_from(headers, now if now is not None else 0.0)
    if retry_after is not None:
        details["retry_after_s"] = retry_after

    message = f"上游返回 HTTP {status}"
    if upstream_message:
        message = f"{message}: {upstream_message}"
    return GatewayError(code, message, details=details)


def map_httpx_error(exc: BaseException) -> GatewayError:
    if isinstance(exc, httpx.TimeoutException):
        return GatewayError("upstream_timeout", f"上游超时: {type(exc).__name__}")
    if isinstance(exc, httpx.HTTPError):
        return GatewayError("upstream_connection_error", f"上游连接失败: {type(exc).__name__}")
    return GatewayError(
        "upstream_connection_error",
        f"上游连接失败: {type(exc).__name__}",
    )


def map_stream_error_payload(payload: dict | None) -> GatewayError:
    """Anthropic / OpenAI 风格流内 error 事件。"""
    error = {}
    if isinstance(payload, dict):
        maybe = payload.get("error")
        if isinstance(maybe, dict):
            error = maybe
        elif isinstance(payload.get("type"), str) and payload.get("type") == "error":
            error = payload
    err_type = str(error.get("type") or error.get("code") or "")
    message = str(error.get("message") or "上游流内错误")
    if "overloaded" in err_type:
        code = "upstream_overloaded"
    elif "rate_limit" in err_type:
        code = "upstream_rate_limited"
    elif "auth" in err_type:
        code = "upstream_auth_error"
    else:
        code = "upstream_server_error"
    return GatewayError(code, message, details={"upstream_message": sanitize(message)})
