"""对外 SSE 的唯一编码处：内部事件在这里变成字节流。"""
from __future__ import annotations

import json
from typing import Any

from gateway.core.errors import GatewayError
from gateway.core.types import Usage

KEEPALIVE_LINE = ": keep-alive\n\n"
DONE_FRAME = "data: [DONE]\n\n"


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def encode_data(payload: dict[str, Any]) -> str:
    return f"data: {_dumps(payload)}\n\n"


def encode_error(error: GatewayError, request_id: str) -> str:
    return f"event: error\ndata: {_dumps(error.envelope(request_id))}\n\n"


def role_payload(request_id: str, created: int, model: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }


def delta_payload(request_id: str, created: int, model: str, content: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


def final_payload(
    request_id: str,
    created: int,
    model: str,
    finish_reason: str,
    usage: Usage,
    gateway: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": usage.as_dict(),
        "gateway": gateway,
    }
