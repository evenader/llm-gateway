"""Anthropic Messages 协议适配器。"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from gateway.core.errors import GatewayError, sanitize
from gateway.core.types import (
    DoneEvent,
    FinishReason,
    StreamEvent,
    TextDelta,
    UnifiedRequest,
    UnifiedResponse,
    Usage,
)
from gateway.providers.base import Provider, UpstreamStream, iter_sse
from gateway.providers.http_errors import map_stream_error_payload


def _int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def map_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict) or not raw:
        return Usage(usage_missing=True)
    input_tokens = _int(raw.get("input_tokens"))
    cache_read = _int(raw.get("cache_read_input_tokens"))
    cache_creation = _int(raw.get("cache_creation_input_tokens"))
    output_tokens = _int(raw.get("output_tokens"))
    if input_tokens is None and cache_read is None and cache_creation is None:
        total_input = None
    else:
        total_input = (input_tokens or 0) + (cache_read or 0) + (cache_creation or 0)
    total = None
    if total_input is not None or output_tokens is not None:
        total = (total_input or 0) + (output_tokens or 0)
    return Usage(
        input_tokens=total_input,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        reasoning_tokens=None,
        total_tokens=total,
    )


_STOP_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "refusal": "content_filter",
}


def _merge_usage(start: Any, final: Any) -> Any:
    if not isinstance(start, dict) and not isinstance(final, dict):
        return None
    merged: dict[str, Any] = dict(start) if isinstance(start, dict) else {}
    if isinstance(final, dict):
        for key, value in final.items():
            if value is not None:
                merged[key] = value
    return merged


class AnthropicMessagesProvider(Provider):
    protocol = "anthropic_messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key(),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def build_body(self, request: UnifiedRequest) -> dict[str, Any]:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        merged: list[dict[str, str]] = []
        for message in request.messages:
            if message.role == "system":
                continue
            role = "assistant" if message.role == "assistant" else "user"
            if merged and merged[-1]["role"] == role:
                merged[-1]["content"] = f"{merged[-1]['content']}\n\n{message.content}"
            else:
                merged.append({"role": role, "content": message.content})
        if not merged or merged[0]["role"] != "user":
            merged.insert(0, {"role": "user", "content": ""})

        max_tokens = request.max_tokens or self.spec.default_max_tokens or 4096
        body: dict[str, Any] = {
            "model": request.upstream_model,
            "max_tokens": max_tokens,
            "messages": merged,
            "stream": request.stream,
        }
        if system_parts:
            body["system"] = "\n".join(system_parts)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.stop:
            body["stop_sequences"] = list(request.stop)
        if request.thinking == "disabled":
            body["thinking"] = {"type": "disabled"}
        return body

    async def complete(self, request: UnifiedRequest) -> UnifiedResponse:
        body = self.build_body(request)
        response = await self._post(self._headers(), body)
        await self._ensure_success(response)
        try:
            data = response.json()
        except (ValueError, UnicodeError) as exc:
            raise GatewayError("upstream_protocol_error", "上游响应不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise GatewayError("upstream_protocol_error", "上游响应不是 JSON 对象")
        if data.get("type") == "error" or isinstance(data.get("error"), dict):
            raise map_stream_error_payload(data)

        content = data.get("content")
        if not isinstance(content, list):
            raise GatewayError("upstream_protocol_error", "上游响应缺少 content")
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                piece = block.get("text")
                if isinstance(piece, str):
                    text_parts.append(piece)
        text = "".join(text_parts)
        if not text:
            raise GatewayError("upstream_empty_response", "上游返回空文本")

        stop_reason = data.get("stop_reason")
        finish: FinishReason
        if stop_reason is None:
            finish = "stop"
        else:
            finish = _STOP_REASONS.get(str(stop_reason), "error")
        usage = map_usage(data.get("usage"))
        return UnifiedResponse(
            text=text,
            finish_reason=finish,
            usage=usage,
            upstream_model=str(data.get("model") or request.upstream_model),
        )

    async def open_stream(self, request: UnifiedRequest) -> UpstreamStream:
        body = self.build_body(request)
        client = self.new_stream_client()
        try:
            response = await self._post(self._headers(), body, stream=True, client=client)
            await self._ensure_success(response)
        except BaseException:
            await client.aclose()
            raise
        return UpstreamStream(response, self._stream_events(response), client=client)

    async def _stream_events(self, response) -> AsyncIterator[StreamEvent]:
        terminated = False
        saw_text = False
        start_usage: Any = None
        final_usage: Any = None
        stop_reason: str | None = None

        async for sse in iter_sse(response):
            if not sse.data:
                continue
            try:
                payload = json.loads(sse.data)
            except ValueError as exc:
                raise GatewayError("upstream_protocol_error", "上游 SSE data 不是合法 JSON") from exc
            if not isinstance(payload, dict):
                continue
            event_type = sse.event or payload.get("type")
            if event_type == "message_start":
                message = payload.get("message")
                if isinstance(message, dict):
                    start_usage = message.get("usage")
            elif event_type == "content_block_delta":
                delta = payload.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        saw_text = True
                        yield TextDelta(text)
            elif event_type == "message_delta":
                delta = payload.get("delta")
                if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                    stop_reason = str(delta.get("stop_reason"))
                final_usage = _merge_usage(start_usage, payload.get("usage"))
            elif event_type == "message_stop":
                if not saw_text:
                    raise GatewayError("upstream_empty_response", "上游流没有返回任何文本")
                finish = _STOP_REASONS.get(stop_reason or "end_turn", "error")
                usage = map_usage(final_usage if final_usage is not None else start_usage)
                terminated = True
                yield DoneEvent(finish_reason=finish, usage=usage)
                return
            elif event_type == "error" or payload.get("type") == "error":
                raise map_stream_error_payload(payload)
            elif event_type in ("ping", "content_block_start", "content_block_stop"):
                continue
        if not terminated:
            raise GatewayError("stream_interrupted", "上游流没有终止事件")
