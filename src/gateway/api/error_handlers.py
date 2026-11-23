"""统一异常处理：所有错误响应使用统一信封。"""
from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.core.errors import GatewayError

logger = logging.getLogger("gateway")


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", None) or uuid.uuid4().hex


def error_json(error: GatewayError, request_id: str) -> tuple[dict[str, Any], dict[str, str]]:
    headers = {"X-Request-ID": request_id}
    if error.code == "rate_limit_exceeded":
        retry_after = error.retry_after_s() or 1.0
        headers["Retry-After"] = str(max(1, int(math.ceil(retry_after))))
    return error.envelope(request_id), headers


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        request_id = request_id_of(request)
        body, headers = error_json(exc, request_id)
        return JSONResponse(body, status_code=exc.http_status, headers=headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        request_id = request_id_of(request)
        logger.error(
            "internal error type=%s request_id=%s", type(exc).__name__, request_id
        )
        error = GatewayError("internal_error", "网关内部错误")
        body, headers = error_json(error, request_id)
        return JSONResponse(body, status_code=500, headers=headers)
