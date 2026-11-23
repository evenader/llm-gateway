"""本地 mock 上游：同时模拟 OpenAI Responses 与 Anthropic Messages，含控制面。

控制面（不走网关）：
  POST /__control   设置按协议的场景（支持按调用序号的 sequence）
  GET  /__stats     调用次数、最近请求头/体、活跃连接数、客户端断开观察、校验违规
  POST /__reset     清零
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

DEFAULT_TEXT = "Hello from mock"

DEFAULT_USAGE_RESPONSES = {
    "input_tokens": 10,
    "output_tokens": 5,
    "total_tokens": 15,
    "input_tokens_details": {"cached_tokens": 2},
    "output_tokens_details": {"reasoning_tokens": 0},
}
DEFAULT_USAGE_ANTHROPIC = {
    "input_tokens": 8,
    "output_tokens": 5,
    "cache_read_input_tokens": 2,
    "cache_creation_input_tokens": 0,
}

FORBIDDEN_RESPONSES_FIELDS = (
    "messages",
    "system",
    "max_tokens",
    "store",
    "previous_response_id",
    "stream_options",
    "metadata",
)


@dataclass
class ProtocolState:
    count: int = 0
    active: int = 0
    disconnected: bool = False
    disconnect_ts: float | None = None
    violations: list[str] = field(default_factory=list)
    last_headers: dict[str, str] = field(default_factory=dict)
    last_body: dict[str, Any] | None = None
    scenario: dict[str, Any] = field(default_factory=dict)
    call_index: int = 0
    timeline: list[float] = field(default_factory=list)
    completed_normally: bool = False


STATE: dict[str, ProtocolState] = {"responses": ProtocolState(), "anthropic": ProtocolState()}


def reset_state() -> None:
    STATE["responses"] = ProtocolState()
    STATE["anthropic"] = ProtocolState()


def _dump_protocol(state: ProtocolState) -> dict[str, Any]:
    return {
        "count": state.count,
        "active": state.active,
        "disconnected": state.disconnected,
        "disconnect_ts": state.disconnect_ts,
        "violations": list(state.violations),
        "last_headers": dict(state.last_headers),
        "last_body": state.last_body,
        "timeline": list(state.timeline),
        "completed_normally": state.completed_normally,
    }


def _scenario(state: ProtocolState) -> dict[str, Any]:
    base = dict(state.scenario or {})
    sequence = base.pop("sequence", None)
    if isinstance(sequence, list) and sequence:
        index = min(state.call_index, len(sequence) - 1)
        extra = sequence[index]
        if isinstance(extra, dict):
            base.update(extra)
    return base


def _sse(event: str | None, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    if event:
        return f"event: {event}\ndata: {data}\n\n"
    return f"data: {data}\n\n"


def _default_chunks(text: str) -> list[str]:
    if text == DEFAULT_TEXT:
        return ["Hello", " from", " mock"]
    return [text]


def _usage_or_none(scen: dict[str, Any], default: dict[str, Any]) -> dict[str, Any] | None:
    if scen.get("omit_usage"):
        return None
    usage = scen.get("usage")
    if isinstance(usage, dict):
        return usage
    return default


def _responses_payload(
    scen: dict[str, Any], text: str, finish: str, usage: dict[str, Any] | None, model: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "resp_mock",
        "object": "response",
        "model": model,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }
    if finish == "stop":
        payload["status"] = "completed"
    else:
        payload["status"] = "incomplete"
        payload["incomplete_details"] = {
            "reason": "max_output_tokens" if finish == "length" else "content_filter"
        }
    if usage is not None:
        payload["usage"] = usage
    return payload


def _anthropic_payload(
    scen: dict[str, Any], text: str, finish: str, usage: dict[str, Any] | None, model: str
) -> dict[str, Any]:
    stop_reason = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}[finish]
    payload: dict[str, Any] = {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def _validate_responses_headers(headers: dict[str, str]) -> list[str]:
    problems: list[str] = []
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or len(auth.strip()) <= len("bearer "):
        problems.append("missing bearer authorization")
    if "application/json" not in headers.get("content-type", ""):
        problems.append("content-type must be application/json")
    return problems


def _validate_responses_body(body: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(body, dict):
        return ["body must be a JSON object"]
    if not isinstance(body.get("model"), str) or not body.get("model"):
        problems.append("model must be a non-empty string")
    if "input" not in body or not isinstance(body.get("input"), list):
        problems.append("input must be a list")
    if "instructions" in body and not isinstance(body.get("instructions"), str):
        problems.append("instructions must be a string")
    if "max_output_tokens" in body and not isinstance(body.get("max_output_tokens"), int):
        problems.append("max_output_tokens must be an integer")
    for forbidden in FORBIDDEN_RESPONSES_FIELDS:
        if forbidden in body:
            problems.append(f"forbidden field: {forbidden}")
    return problems


def _validate_anthropic_headers(headers: dict[str, str]) -> list[str]:
    problems: list[str] = []
    if not headers.get("x-api-key"):
        problems.append("missing x-api-key")
    if headers.get("anthropic-version") != "2023-06-01":
        problems.append("invalid anthropic-version")
    if "application/json" not in headers.get("content-type", ""):
        problems.append("content-type must be application/json")
    return problems


def _validate_anthropic_body(body: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(body, dict):
        return ["body must be a JSON object"]
    if not isinstance(body.get("model"), str) or not body.get("model"):
        problems.append("model must be a non-empty string")
    if not isinstance(body.get("max_tokens"), int) or body.get("max_tokens", 0) <= 0:
        problems.append("max_tokens must be a positive integer")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        problems.append("messages must be a non-empty list")
    else:
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                problems.append(f"messages[{index}] must be an object")
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                problems.append(f"messages[{index}] invalid role: {role}")
            if not isinstance(message.get("content"), str):
                problems.append(f"messages[{index}].content must be a string")
        if messages and isinstance(messages[0], dict) and messages[0].get("role") != "user":
            problems.append("messages must start with user")
    if "system" in body and not isinstance(body.get("system"), str):
        problems.append("system must be a string")
    if "response_format" in body or "text" in body:
        problems.append("anthropic protocol must not receive response_format/text")
    thinking = body.get("thinking")
    if thinking is not None and thinking != {"type": "disabled"}:
        problems.append("thinking must be disabled")
    return problems


async def _read_body(request: Request) -> Any:
    try:
        return await request.json()
    except (ValueError, UnicodeError):
        return None


def _record_headers(state: ProtocolState, request: Request) -> dict[str, str]:
    headers = {key.lower(): value for key, value in request.headers.items()}
    state.last_headers = headers
    return headers


def _retry_after_headers(scen: dict[str, Any]) -> dict[str, str]:
    if scen.get("retry_after") is None:
        return {}
    return {"Retry-After": str(scen["retry_after"])}


async def _status_response(scen: dict[str, Any]) -> JSONResponse:
    status = int(scen.get("status", 500))
    message = str(scen.get("error_message") or f"mock upstream status {status}")
    return JSONResponse(
        {"error": {"type": "mock_error", "message": message}},
        status_code=status,
        headers=_retry_after_headers(scen),
    )


def _should_delay(scen: dict[str, Any]) -> float:
    return float(scen.get("delay_ms", 0)) / 1000.0


async def _non_stream_responses(scen: dict[str, Any], model: str = "mock-model", structured: bool = False) -> Any:
    kind = scen.get("kind", "ok")
    if kind == "status":
        return await _status_response(scen)
    if kind == "hang":
        await asyncio.sleep(float(scen.get("hang_s", 10)))
        return JSONResponse({"error": {"message": "hung"}}, status_code=504)
    delay = _should_delay(scen)
    if delay:
        await asyncio.sleep(delay)
    if kind == "non_json":
        return PlainTextResponse("this-is-not-json", status_code=200)
    if kind == "missing_output":
        return JSONResponse({"id": "resp_mock", "status": "completed"})
    if "text" in scen:
        text = str(scen["text"])
    elif kind == "empty_text":
        text = ""
    elif structured:
        text = '{"ok": true}'
    else:
        text = DEFAULT_TEXT
    finish = str(scen.get("finish", "stop"))
    usage = _usage_or_none(scen, DEFAULT_USAGE_RESPONSES)
    return JSONResponse(_responses_payload(scen, text, finish, usage, model))


async def _non_stream_anthropic(scen: dict[str, Any], model: str = "mock-model", structured: bool = False) -> Any:
    kind = scen.get("kind", "ok")
    if kind == "status":
        return await _status_response(scen)
    if kind == "hang":
        await asyncio.sleep(float(scen.get("hang_s", 10)))
        return JSONResponse({"type": "error", "error": {"message": "hung"}}, status_code=504)
    delay = _should_delay(scen)
    if delay:
        await asyncio.sleep(delay)
    if kind == "non_json":
        return PlainTextResponse("this-is-not-json", status_code=200)
    if kind == "missing_output":
        return JSONResponse({"id": "msg_mock", "type": "message"})
    if "text" in scen:
        text = str(scen["text"])
    elif kind == "empty_text":
        text = ""
    elif structured:
        text = '{"ok": true}'
    else:
        text = DEFAULT_TEXT
    finish = str(scen.get("finish", "stop"))
    usage = _usage_or_none(scen, DEFAULT_USAGE_ANTHROPIC)
    return JSONResponse(_anthropic_payload(scen, text, finish, usage, model))


async def _stream_responses(scen: dict[str, Any], state: ProtocolState) -> StreamingResponse:
    text = str(scen.get("text", DEFAULT_TEXT))
    chunks = scen.get("chunks") if isinstance(scen.get("chunks"), list) else _default_chunks(text)
    first_delay = float(scen.get("first_delay_ms", 0)) / 1000.0
    interval = float(scen.get("chunk_interval_ms", 0)) / 1000.0
    delays = scen.get("chunk_delays_ms")
    close_after = scen.get("close_after_chunks")
    no_terminator = bool(scen.get("no_terminator"))
    finish = str(scen.get("finish", "stop"))
    usage = _usage_or_none(scen, DEFAULT_USAGE_RESPONSES)

    async def gen():
        state.active += 1
        completed = False
        try:
            yield _sse("response.created", {"type": "response.created", "response": {"status": "in_progress"}})
            if first_delay:
                await asyncio.sleep(first_delay)
            sent = 0
            for index, piece in enumerate(chunks):
                delay = interval
                if isinstance(delays, list) and index < len(delays):
                    delay = float(delays[index]) / 1000.0
                if delay:
                    await asyncio.sleep(delay)
                state.timeline.append(time.monotonic())
                yield _sse("response.output_text.delta", {"type": "response.output_text.delta", "delta": piece})
                sent += 1
                if close_after is not None and sent >= int(close_after):
                    completed = True
                    return
            if no_terminator:
                completed = True
                return
            if finish == "stop":
                status = "completed"
                response_obj: dict[str, Any] = {"status": status}
                event_name = "response.completed"
            else:
                response_obj = {
                    "status": "incomplete",
                    "incomplete_details": {
                        "reason": "max_output_tokens" if finish == "length" else "content_filter"
                    },
                }
                event_name = "response.incomplete"
            if usage is not None:
                response_obj["usage"] = usage
            yield _sse(event_name, {"type": event_name, "response": response_obj})
            completed = True
        finally:
            state.active -= 1
            if not completed:
                state.disconnected = True
                state.disconnect_ts = time.monotonic()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


async def _stream_anthropic(scen: dict[str, Any], state: ProtocolState) -> StreamingResponse:
    text = str(scen.get("text", DEFAULT_TEXT))
    chunks = scen.get("chunks") if isinstance(scen.get("chunks"), list) else _default_chunks(text)
    first_delay = float(scen.get("first_delay_ms", 0)) / 1000.0
    interval = float(scen.get("chunk_interval_ms", 0)) / 1000.0
    delays = scen.get("chunk_delays_ms")
    close_after = scen.get("close_after_chunks")
    no_terminator = bool(scen.get("no_terminator"))
    finish = str(scen.get("finish", "stop"))
    usage = _usage_or_none(scen, DEFAULT_USAGE_ANTHROPIC)

    start_usage = scen.get("stream_start_usage")
    if start_usage is None and usage is not None:
        start_usage = dict(usage)
        start_usage["output_tokens"] = 1
    if scen.get("omit_usage"):
        start_usage = None
    delta_usage = scen.get("stream_delta_usage")
    if delta_usage is None and usage is not None:
        delta_usage = {"output_tokens": usage.get("output_tokens")}
    if scen.get("omit_usage"):
        delta_usage = None
    stop_reason = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}[finish]

    async def gen():
        state.active += 1
        completed = False
        try:
            message: dict[str, Any] = {"id": "msg_mock", "type": "message", "role": "assistant", "model": "mock-model"}
            if start_usage is not None:
                message["usage"] = start_usage
            yield _sse("message_start", {"type": "message_start", "message": message})
            yield _sse(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            )
            yield _sse("ping", {"type": "ping"})
            if first_delay:
                await asyncio.sleep(first_delay)
            sent = 0
            for index, piece in enumerate(chunks):
                delay = interval
                if isinstance(delays, list) and index < len(delays):
                    delay = float(delays[index]) / 1000.0
                if delay:
                    await asyncio.sleep(delay)
                state.timeline.append(time.monotonic())
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": piece},
                    },
                )
                sent += 1
                if close_after is not None and sent >= int(close_after):
                    completed = True
                    return
            if no_terminator:
                completed = True
                return
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            delta_payload: dict[str, Any] = {"delta": {"stop_reason": stop_reason}}
            if delta_usage is not None:
                delta_payload["usage"] = delta_usage
            yield _sse("message_delta", {"type": "message_delta", **delta_payload})
            yield _sse("message_stop", {"type": "message_stop"})
            completed = True
        finally:
            state.active -= 1
            if not completed:
                state.disconnected = True
                state.disconnect_ts = time.monotonic()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def create_mock_app() -> FastAPI:
    app = FastAPI(title="mock upstream")

    @app.post("/__control")
    async def control(request: Request):
        body = await _read_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        for key in ("responses", "anthropic"):
            if key in body:
                scenario = body[key] if isinstance(body[key], dict) else {}
                state = STATE[key]
                state.scenario = scenario
                state.call_index = 0
                state.timeline = []
                state.violations = []
                state.disconnected = False
                state.completed_normally = False
        return {"ok": True, "scenario": {key: STATE[key].scenario for key in STATE}}

    @app.get("/__stats")
    async def stats():
        return {key: _dump_protocol(STATE[key]) for key in STATE}

    @app.post("/__reset")
    async def reset():
        reset_state()
        return {"ok": True}

    @app.post("/responses")
    async def responses(request: Request):
        state = STATE["responses"]
        state.count += 1
        headers = _record_headers(state, request)
        body = await _read_body(request)
        state.last_body = body if isinstance(body, dict) else {"_raw": str(body)}
        problems = _validate_responses_headers(headers) + _validate_responses_body(body)
        if problems:
            state.violations.extend(problems)
            return JSONResponse({"error": {"message": "mock protocol violation", "details": problems}}, status_code=400)
        scen = _scenario(state)
        state.call_index += 1
        if bool(body.get("stream")):
            kind = scen.get("kind", "ok")
            if kind == "status":
                return await _status_response(scen)
            if kind == "hang":
                await asyncio.sleep(float(scen.get("hang_s", 10)))
                return JSONResponse({"error": {"message": "hung"}}, status_code=504)
            if kind == "non_json":
                return PlainTextResponse("this-is-not-json", status_code=200)
            return await _stream_responses(scen, state)
        structured = isinstance(body.get("text"), dict)
        return await _non_stream_responses(scen, str(body.get("model") or "mock-model"), structured)

    @app.post("/anthropic/v1/messages")
    async def anthropic(request: Request):
        state = STATE["anthropic"]
        state.count += 1
        headers = _record_headers(state, request)
        body = await _read_body(request)
        state.last_body = body if isinstance(body, dict) else {"_raw": str(body)}
        problems = _validate_anthropic_headers(headers) + _validate_anthropic_body(body)
        if problems:
            state.violations.extend(problems)
            return JSONResponse({"type": "error", "error": {"message": str(problems)}}, status_code=400)
        scen = _scenario(state)
        state.call_index += 1
        if bool(body.get("stream")):
            kind = scen.get("kind", "ok")
            if kind == "status":
                return await _status_response(scen)
            if kind == "hang":
                await asyncio.sleep(float(scen.get("hang_s", 10)))
                return JSONResponse({"type": "error", "error": {"message": "hung"}}, status_code=504)
            if kind == "non_json":
                return PlainTextResponse("this-is-not-json", status_code=200)
            return await _stream_anthropic(scen, state)
        structured = "JSON" in str(body.get("system") or "")
        return await _non_stream_anthropic(scen, str(body.get("model") or "mock-model"), structured)

    return app
