"""统一错误类型与错误信封。

GatewayError 是网关唯一的业务错误类型；错误码表见 docs/blueprint/03-error-codes.md。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorSpec:
    http_status: int
    stage: str
    retryable: bool
    type: str


ERROR_SPECS: dict[str, ErrorSpec] = {
    "invalid_request": ErrorSpec(422, "request", False, "invalid_request_error"),
    "unsupported_parameter_combination": ErrorSpec(400, "request", False, "invalid_request_error"),
    "invalid_response_format": ErrorSpec(422, "request", False, "invalid_request_error"),
    "unsupported_capability": ErrorSpec(400, "routing", False, "invalid_request_error"),
    "model_not_found": ErrorSpec(404, "routing", False, "invalid_request_error"),
    "auth_failed": ErrorSpec(401, "admission", False, "gateway_error"),
    "rate_limit_exceeded": ErrorSpec(429, "admission", False, "rate_limit_error"),
    "template_not_found": ErrorSpec(404, "template", False, "invalid_request_error"),
    "template_variable_missing": ErrorSpec(422, "template", False, "invalid_request_error"),
    "template_render_error": ErrorSpec(422, "template", False, "invalid_request_error"),
    "template_conflict": ErrorSpec(409, "template", False, "invalid_request_error"),
    "upstream_connection_error": ErrorSpec(502, "connect", True, "upstream_error"),
    "upstream_timeout": ErrorSpec(504, "connect", True, "upstream_error"),
    "upstream_rate_limited": ErrorSpec(503, "upstream_http", True, "upstream_error"),
    "upstream_server_error": ErrorSpec(502, "upstream_http", True, "upstream_error"),
    "upstream_overloaded": ErrorSpec(503, "upstream_http", True, "upstream_error"),
    "upstream_auth_error": ErrorSpec(502, "upstream_http", False, "upstream_error"),
    "upstream_insufficient_balance": ErrorSpec(502, "upstream_http", False, "upstream_error"),
    "upstream_bad_request": ErrorSpec(502, "upstream_http", False, "upstream_error"),
    "upstream_protocol_error": ErrorSpec(502, "protocol", False, "upstream_error"),
    "upstream_empty_response": ErrorSpec(502, "protocol", False, "upstream_error"),
    "stream_interrupted": ErrorSpec(502, "stream", False, "upstream_error"),
    "structured_output_invalid": ErrorSpec(502, "output_validation", False, "structured_output_error"),
    "structured_output_truncated": ErrorSpec(502, "output_validation", False, "structured_output_error"),
    "internal_error": ErrorSpec(500, "internal", False, "gateway_error"),
    "trace_not_found": ErrorSpec(404, "internal", False, "gateway_error"),
}

_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_API_KEY = re.compile(r"(?i)(x-api-key|authorization)\s*[:=]\s*\S+")
_SK_KEY = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")


def sanitize(text: str | None, limit: int = 300) -> str:
    """去除 Key / 鉴权头 / 长随机串，压缩空白并截断。"""
    if not text:
        return ""
    out = str(text)
    out = _BEARER.sub("Bearer [redacted]", out)
    out = _API_KEY.sub(lambda m: f"{m.group(1)}: [redacted]", out)
    out = _SK_KEY.sub("sk-[redacted]", out)
    out = _LONG_TOKEN.sub("[redacted]", out)
    out = " ".join(out.split())
    return out[:limit]


class GatewayError(Exception):
    """网关唯一错误类型。"""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
        http_status: int | None = None,
        stage: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        spec = ERROR_SPECS.get(code, ERROR_SPECS["internal_error"])
        self.code = code
        self.message = sanitize(message) if message else code
        self.http_status = spec.http_status if http_status is None else http_status
        self.stage = spec.stage if stage is None else stage
        self.retryable = spec.retryable if retryable is None else retryable
        self.type = spec.type
        self.details = details or {}
        super().__init__(f"{code}: {self.message}")

    def envelope(self, request_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "type": self.type,
            "stage": self.stage,
            "retryable": self.retryable,
            "request_id": request_id,
        }
        if self.details:
            body["details"] = self.details
        return {"error": body}

    def retry_after_s(self) -> float | None:
        value = self.details.get("retry_after_s")
        if isinstance(value, (int, float)):
            return float(value)
        return None
