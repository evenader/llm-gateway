"""OpenAI Responses 协议适配器。"""
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


def _int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def map_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict) or not raw:
        return Usage(usage_missing=True)
    input_tokens = _int(raw.get("input_tokens"))
    output_tokens = _int(raw.get("output_tokens"))
    input_details = raw.get("input_tokens_details")
    output_details = raw.get("output_tokens_details")
    cache_read = _int(input_details.get("cached_tokens")) if isinstance(input_details, dict) else None
    reasoning = _int(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
    total = _int(raw.get("total_tokens"))
    if total is None and (input_tokens is not None or output_tokens is not None):
        total = (input_tokens or 0) + (output_tokens or 0)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        reasoning_tokens=reasoning,
        total_tokens=total,
    )


class OpenAIResponsesProvider(Provider):
    protocol = "openai_responses"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def build_body(self, request: UnifiedRequest) -> dict[str, Any]:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        others = [m for m in request.messages if m.role != "system"]
        body: dict[str, Any] = {
            "model": request.upstream_model,
            "input": [{"role": m.role, "content": m.content} for m in others],
            "stream": request.stream,
        }
        if system_parts:
            body["instructions"] = "\n".join(system_parts)
        if request.max_tokens is not None:
            body["max_output_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        fmt = request.response_format
        if fmt.type == "json_object":
            body["text"] = {"format": {"type": "json_object"}}
        elif fmt.type == "json_schema" and fmt.json_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": fmt.json_schema.name,
                    "schema": fmt.json_schema.schema,
                    "strict": fmt.json_schema.strict,
                }
            }
        if request.thinking == "disabled":
            body["reasoning"] = {"effort": "none"}
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

        status = data.get("status")
        if status == "failed" or data.get("error"):
            error = data.get("error")
            message = ""
            if isinstance(error, dict):
                message = str(error.get("message") or "")
            raise GatewayError(
                "upstream_protocol_error",
                f"上游声明失败: {message}",
                details={"upstream_message": sanitize(message)},
            )
        output = data.get("output")
        if not isinstance(output, list) or status not in ("completed", "incomplete"):
            raise GatewayError("upstream_protocol_error", "上游响应缺少 output 或 status")

        text_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    piece = block.get("text")
                    if isinstance(piece, str):
                        text_parts.append(piece)
        text = "".join(text_parts)

        finish = self._finish_reason(status, data)
        usage = map_usage(data.get("usage"))
        if not text:
            raise GatewayError("upstream_empty_response", "上游返回空文本")
        return UnifiedResponse(text=text, finish_reason=finish, usage=usage, upstream_model=str(data.get("model") or request.upstream_model))

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
            if event_type == "response.output_text.delta":
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    saw_text = True
                    yield TextDelta(delta)
            elif event_type in ("response.completed", "response.incomplete"):
                response_obj = payload.get("response")
                if not isinstance(response_obj, dict):
                    response_obj = {}
                status = response_obj.get("status") or (
                    "completed" if event_type == "response.completed" else "incomplete"
                )
                finish = self._finish_reason(status, response_obj)
                usage = map_usage(response_obj.get("usage"))
                if not saw_text:
                    raise GatewayError("upstream_empty_response", "上游流没有返回任何文本")
                terminated = True
                yield DoneEvent(finish_reason=finish, usage=usage)
                return
            elif event_type == "response.failed":
                response_obj = payload.get("response") if isinstance(payload.get("response"), dict) else {}
                error = response_obj.get("error") if isinstance(response_obj, dict) else None
                message = str(error.get("message")) if isinstance(error, dict) else "上游声明失败"
                raise GatewayError(
                    "upstream_protocol_error",
                    f"上游声明失败: {message}",
                    details={"upstream_message": sanitize(message)},
                )
        if not terminated:
            raise GatewayError("stream_interrupted", "上游流没有终止事件")

    @staticmethod
    def _finish_reason(status: str, payload: dict[str, Any]) -> FinishReason:
        if status == "completed":
            return "stop"
        if status == "incomplete":
            details = payload.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            if reason == "max_output_tokens":
                return "length"
            return "content_filter"
        raise GatewayError("upstream_protocol_error", f"未知上游状态: {status}")
