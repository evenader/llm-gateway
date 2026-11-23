"""SSE 输出：把 StreamSession 的内部条目编码为对外字节流。"""
from __future__ import annotations

from collections.abc import AsyncIterator

from gateway.api.sse import (
    DONE_FRAME,
    KEEPALIVE_LINE,
    delta_payload,
    encode_data,
    encode_error,
    final_payload,
    role_payload,
)
from gateway.core.types import Usage


async def encode_session(session) -> AsyncIterator[str]:
    try:
        yield encode_data(role_payload(session.request_id, session.created, session.model))
        async for item in session:
            if item.kind == "keepalive":
                yield KEEPALIVE_LINE
            elif item.kind == "delta":
                yield encode_data(delta_payload(session.request_id, session.created, session.model, item.text))
            elif item.kind == "final":
                yield encode_data(
                    final_payload(
                        session.request_id,
                        session.created,
                        session.model,
                        item.finish_reason or "stop",
                        item.usage or Usage(usage_missing=True),
                        item.gateway or {},
                    )
                )
                yield DONE_FRAME
            elif item.kind == "error" and item.error is not None:
                yield encode_error(item.error, session.request_id)
    finally:
        # 正常结束、连接取消或发送失败：都确保上游被关闭
        try:
            await session.abort()
        except Exception:
            pass
