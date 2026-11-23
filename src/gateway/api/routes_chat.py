"""POST /v1/chat/completions 统一入口。"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.api.routes_sse_stream import encode_session
from gateway.core.errors import GatewayError

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise GatewayError("invalid_request", "请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise GatewayError("invalid_request", "请求体必须是 JSON 对象")

    service = request.app.state.chat_service
    result = await service.handle(payload, request_id)

    if result.stream is not None:
        return StreamingResponse(
            encode_session(result.stream),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-ID": request_id,
            },
        )
    return JSONResponse(content=result.body, headers={"X-Request-ID": request_id})
