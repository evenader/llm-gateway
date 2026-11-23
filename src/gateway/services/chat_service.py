"""聊天用例编排：入口校验 → 路由 → 准入 → 模板 → 能力 → 上游（重试/修复）→ 响应与事件。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from pydantic import ValidationError

from gateway.api.schemas import (
    ChatRequest,
    to_internal_messages,
    to_internal_response_format,
    validation_fields,
)
from gateway.config import ModelConfig, Settings
from gateway.core.clock import Clock
from gateway.core.errors import GatewayError
from gateway.core.events import (
    AttemptFinished,
    EventBus,
    FirstTokenReceived,
    RequestFinished,
    RequestStarted,
)
from gateway.core.ratelimit import RateLimiter
from gateway.core.retry import AttemptRecord, Retrier
from gateway.core.types import (
    DoneEvent,
    Message,
    ResponseFormat,
    TextDelta,
    UnifiedRequest,
    UnifiedResponse,
    Usage,
)
from gateway.providers.base import Provider, UpstreamStream
from gateway.services.router import Router
from gateway.services.structured import (
    build_repair_instruction,
    build_structured_instruction,
    validate_output,
    validate_response_format_schema,
)


@dataclass
class _RunState:
    request_id: str
    ts_start: float
    started_perf: float
    created: int
    logical_model: str | None = None
    upstream_model: str | None = None
    spec: ModelConfig | None = None
    stream: bool = False
    prompt_name: str | None = None
    prompt_version: str | None = None
    prompt_fingerprint: str | None = None
    prompt_chars: int | None = None
    attempts: list[AttemptRecord] = field(default_factory=list)
    repair_count: int = 0
    usage: Usage | None = None
    ttft_ms: float | None = None
    emitted_chars: int = 0
    output_chars: int = 0
    pre_upstream_ms: float = 0.0
    finish_reason: str | None = None
    finalized: bool = False

    @property
    def retry_count(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.backoff_ms > 0)

    @property
    def retry_wait_ms(self) -> float:
        return sum(attempt.backoff_ms for attempt in self.attempts)


@dataclass
class SSEItem:
    kind: str  # role | delta | final | error | keepalive
    text: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None
    gateway: dict[str, Any] | None = None
    error: GatewayError | None = None


@dataclass
class ChatResult:
    body: dict[str, Any] | None = None
    stream: "StreamSession | None" = None


FinalizeCallback = Callable[[str, GatewayError | None], None]
GatewayMetaBuilder = Callable[[float], dict[str, Any]]


class StreamSession:
    """提交点之后的流式会话：泵上游事件 → 队列 → SSE 条目；负责取消与终态收尾。"""

    def __init__(
        self,
        *,
        state: _RunState,
        upstream: UpstreamStream,
        first_event: TextDelta | DoneEvent,
        heartbeat_interval_s: float,
        finalize: FinalizeCallback,
        build_gateway: GatewayMetaBuilder,
        bus: EventBus,
    ) -> None:
        self.state = state
        self.request_id = state.request_id
        self.created = state.created
        self.model = state.logical_model or ""
        self._upstream = upstream
        self._first = first_event
        self._heartbeat = heartbeat_interval_s
        self._finalize = finalize
        self._build_gateway = build_gateway
        self._bus = bus
        self._queue: asyncio.Queue[SSEItem] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None

    async def abort(self) -> None:
        """客户端断开：停止泵、释放上游，让 SSE 生成器退出并记 cancelled。"""
        self._queue.put_nowait(SSEItem("abort"))
        self._finalize("cancelled", None)
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        try:
            await asyncio.shield(self._cleanup_task)
        except BaseException:
            pass

    async def _cleanup(self) -> None:
        # 先关闭网络响应（泵可能仍在读取），再取消泵，避免 httpx 把半读连接放回连接池
        await self._upstream.aclose()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _pump(self) -> None:
        try:
            first = self._first
            if isinstance(first, TextDelta):
                await self._queue.put(SSEItem("delta", text=first.text))
            elif isinstance(first, DoneEvent):
                await self._queue.put(
                    SSEItem("final", finish_reason=first.finish_reason, usage=first.usage)
                )
                return
            async for event in self._upstream:
                if isinstance(event, TextDelta):
                    if event.text:
                        await self._queue.put(SSEItem("delta", text=event.text))
                elif isinstance(event, DoneEvent):
                    await self._queue.put(
                        SSEItem("final", finish_reason=event.finish_reason, usage=event.usage)
                    )
                    return
            raise GatewayError("stream_interrupted", "上游流没有终止事件")
        except asyncio.CancelledError:
            raise
        except GatewayError as exc:
            await self._queue.put(SSEItem("error", error=exc))
        except Exception as exc:  # 未预期异常不影响收尾路径
            await self._queue.put(
                SSEItem("error", error=GatewayError("internal_error", f"流式处理异常: {type(exc).__name__}"))
            )

    async def __aiter__(self):
        task = asyncio.create_task(self._pump())
        self._task = task
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=self._heartbeat)
                except asyncio.TimeoutError:
                    yield SSEItem("keepalive")
                    continue

                if item.kind == "abort":
                    return
                if item.kind == "delta":
                    self.state.emitted_chars += len(item.text)
                    if self.state.ttft_ms is None and item.text:
                        self.state.ttft_ms = (time.perf_counter() - self.state.started_perf) * 1000
                        self._bus.emit(
                            FirstTokenReceived(self.request_id, round(self.state.ttft_ms, 3))
                        )
                    yield item
                elif item.kind == "final":
                    self.state.finish_reason = item.finish_reason
                    self.state.usage = item.usage
                    self.state.output_chars = self.state.emitted_chars
                    latency_ms = (time.perf_counter() - self.state.started_perf) * 1000
                    item.gateway = self._build_gateway(latency_ms)
                    self._finalize("completed", None)
                    yield item
                    return
                else:  # error
                    self._finalize("failed", item.error)
                    yield item
                    return
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            close_task = asyncio.create_task(self._upstream.aclose())
            try:
                await asyncio.shield(close_task)
            except BaseException:
                pass
            self._finalize("cancelled", None)


class ChatService:
    def __init__(
        self,
        *,
        settings: Settings,
        router: Router,
        providers: dict[str, Provider],
        limiter: RateLimiter,
        retrier: Retrier,
        bus: EventBus,
        clock: Clock,
        prompt_service: Any | None = None,
    ) -> None:
        self._settings = settings
        self._router = router
        self._providers = providers
        self._limiter = limiter
        self._retrier = retrier
        self._bus = bus
        self._clock = clock
        self._prompt_service = prompt_service

    @property
    def providers(self) -> dict[str, Provider]:
        return self._providers

    async def handle(self, payload: dict[str, Any], request_id: str) -> ChatResult:
        started_perf = time.perf_counter()
        ts_start = time.time()
        state = _RunState(
            request_id=request_id,
            ts_start=ts_start,
            started_perf=started_perf,
            created=int(ts_start),
        )
        self._bus.emit(RequestStarted(request_id, ts_start))
        try:
            chat = self._parse(payload)
            state.logical_model = chat.model
            state.stream = chat.stream
            fmt = to_internal_response_format(chat.response_format)
            validate_response_format_schema(fmt)
            self._validate_combinations(chat, fmt)

            decision = self._router.resolve(chat.model)
            spec = decision.spec
            state.spec = spec

            limit = self._settings.rate_limit_for(spec)
            bucket = self._limiter.bucket(chat.model, limit.rate_per_s, limit.burst)
            acquired, retry_after = bucket.try_acquire()
            if not acquired:
                raise GatewayError(
                    "rate_limit_exceeded",
                    f"模型 {chat.model} 超出限流",
                    details={
                        "model": chat.model,
                        "rate_per_s": limit.rate_per_s,
                        "burst": limit.burst,
                        "retry_after_s": retry_after,
                    },
                )

            messages = self._prepare_messages(chat, state)
            if chat.stream and not spec.capabilities.stream:
                raise GatewayError(
                    "unsupported_capability",
                    f"模型 {chat.model} 不支持流式输出",
                    details={"model": chat.model, "capability": "stream"},
                )
            if fmt.is_structured and not spec.capabilities.structured_output:
                raise GatewayError(
                    "unsupported_capability",
                    f"模型 {chat.model} 不支持结构化输出",
                    details={"model": chat.model, "capability": "structured_output"},
                )
            if fmt.is_structured and not self._native_structured(spec, fmt):
                messages = messages + [Message(role="system", content=build_structured_instruction(fmt))]

            unified = self._build_unified(chat, spec, messages, fmt)
            deadline = self._clock.monotonic() + self._settings.timeout_for(spec).total_s

            if chat.stream:
                return await self._start_stream(state, unified, deadline)
            return await self._run_non_stream(state, unified, fmt, deadline)
        except GatewayError as exc:
            self._finalize(state, "rejected" if exc.code == "rate_limit_exceeded" else "failed", exc)
            raise
        except Exception as exc:
            internal = GatewayError("internal_error", f"网关内部错误: {type(exc).__name__}")
            self._finalize(state, "failed", internal)
            raise internal from exc

    # ------------------------------------------------------------------ 校验与构造

    @staticmethod
    def _parse(payload: dict[str, Any]) -> ChatRequest:
        try:
            return ChatRequest.model_validate(payload)
        except ValidationError as exc:
            raise GatewayError(
                "invalid_request",
                "请求字段校验失败",
                details={"fields": validation_fields(exc)},
            ) from exc

    @staticmethod
    def _validate_combinations(chat: ChatRequest, fmt: ResponseFormat) -> None:
        if chat.messages is None and chat.prompt is None:
            raise GatewayError(
                "invalid_request",
                "messages 与 prompt 至少提供一个",
                details={"fields": [{"path": "messages", "message": "messages 与 prompt 至少提供一个"}]},
            )
        if chat.messages is not None and len(chat.messages) == 0:
            raise GatewayError(
                "invalid_request",
                "messages 不能为空",
                details={"fields": [{"path": "messages", "message": "不能为空"}]},
            )
        if chat.prompt is not None and chat.messages:
            if any(message.role == "system" for message in chat.messages):
                raise GatewayError(
                    "invalid_request",
                    "引用模板时 messages 不能包含 system 角色",
                    details={"fields": [{"path": "messages", "message": "不能包含 system 角色"}]},
                )
        if chat.stream and fmt.is_structured:
            raise GatewayError(
                "unsupported_parameter_combination",
                "stream 与 response_format 不能同时使用",
                details={"fields": ["stream", "response_format"]},
            )

    def _prepare_messages(self, chat: ChatRequest, state: _RunState) -> list[Message]:
        messages = to_internal_messages(chat.messages or [])
        if chat.prompt is None:
            return messages
        if self._prompt_service is None:
            raise GatewayError(
                "template_not_found",
                f"模板 {chat.prompt.name} 不存在",
                details={"name": chat.prompt.name},
            )
        rendered = self._prompt_service.render(
            chat.prompt.name, chat.prompt.version, chat.prompt.variables
        )
        state.prompt_name = rendered.name
        state.prompt_version = rendered.version
        state.prompt_fingerprint = rendered.fingerprint
        state.prompt_chars = rendered.chars
        return [Message(role=item["role"], content=item["content"]) for item in rendered.messages] + messages

    @staticmethod
    def _native_structured(spec: ModelConfig, fmt: ResponseFormat) -> bool:
        if fmt.type == "json_object":
            return bool(spec.capabilities.native_json_object)
        if fmt.type == "json_schema":
            return bool(spec.capabilities.native_json_schema)
        return False

    @staticmethod
    def _build_unified(
        chat: ChatRequest,
        spec: ModelConfig,
        messages: list[Message],
        fmt: ResponseFormat,
    ) -> UnifiedRequest:
        return UnifiedRequest(
            messages=tuple(messages),
            upstream_model=spec.upstream_model,
            temperature=chat.temperature if chat.temperature is not None else spec.default_temperature,
            top_p=chat.top_p if chat.top_p is not None else spec.default_top_p,
            max_tokens=chat.max_tokens if chat.max_tokens is not None else spec.default_max_tokens,
            stop=tuple(chat.stop or ()),
            response_format=fmt,
            stream=chat.stream,
            thinking=spec.thinking,
        )

    # ------------------------------------------------------------------ 上游调用

    def _on_attempt(self, state: _RunState, record: AttemptRecord) -> None:
        state.attempts.append(record)
        self._bus.emit(AttemptFinished(state.request_id, record.as_dict()))

    async def _call_once(
        self, state: _RunState, provider: Provider, request: UnifiedRequest, deadline: float
    ) -> UnifiedResponse:
        result = await self._retrier.run(
            lambda: provider.complete(request),
            deadline=deadline,
            on_attempt=lambda record: self._on_attempt(state, record),
        )
        return result.value

    async def _run_non_stream(
        self,
        state: _RunState,
        unified: UnifiedRequest,
        fmt: ResponseFormat,
        deadline: float,
    ) -> ChatResult:
        assert state.logical_model is not None
        provider = self._providers[state.logical_model]
        state.pre_upstream_ms = (time.perf_counter() - state.started_perf) * 1000

        parsed: Any | None = None
        if fmt.is_structured:
            text, parsed = await self._structured_complete(state, provider, unified, fmt, deadline)
        else:
            response = await self._call_once(state, provider, unified, deadline)
            state.usage = response.usage
            state.finish_reason = response.finish_reason
            state.upstream_model = response.upstream_model
            text = response.text

        state.output_chars = len(text)
        latency_ms = (time.perf_counter() - state.started_perf) * 1000
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if fmt.is_structured and parsed is not None:
            message["parsed"] = parsed
        body = {
            "id": state.request_id,
            "object": "chat.completion",
            "created": state.created,
            "model": state.logical_model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": state.finish_reason,
                }
            ],
            "usage": (state.usage or Usage(usage_missing=True)).as_dict(),
            "gateway": self._gateway_meta(state, latency_ms),
        }
        self._finalize(state, "completed", None)
        return ChatResult(body=body)

    async def _structured_complete(
        self,
        state: _RunState,
        provider: Provider,
        unified: UnifiedRequest,
        fmt: ResponseFormat,
        deadline: float,
    ) -> tuple[str, Any]:
        max_repairs = self._settings.structured.max_repairs
        messages = list(unified.messages)
        total_usage = Usage()
        repairs = 0
        while True:
            request = replace(unified, messages=tuple(messages))
            response = await self._call_once(state, provider, request, deadline)
            total_usage = total_usage.add(response.usage)
            parsed, errors = validate_output(response.text, fmt)
            if not errors:
                state.usage = total_usage
                state.repair_count = repairs
                state.finish_reason = response.finish_reason
                state.upstream_model = response.upstream_model
                return response.text, parsed
            if response.finish_reason == "length":
                state.usage = total_usage
                state.repair_count = repairs
                state.finish_reason = response.finish_reason
                raise GatewayError(
                    "structured_output_truncated",
                    "结构化输出因 max_tokens 截断，修复无意义",
                    details={"finish_reason": "length", "repair_count": repairs},
                )
            if repairs >= max_repairs:
                state.usage = total_usage
                state.repair_count = repairs
                state.finish_reason = response.finish_reason
                raise GatewayError(
                    "structured_output_invalid",
                    "结构化输出修复次数用尽仍不合法",
                    details={"errors": errors[:20], "repair_count": repairs},
                )
            repairs += 1
            messages = messages + [
                Message(role="assistant", content=response.text),
                Message(role="user", content=build_repair_instruction(errors)),
            ]

    async def _start_stream(
        self,
        state: _RunState,
        unified: UnifiedRequest,
        deadline: float,
    ) -> ChatResult:
        assert state.logical_model is not None and state.spec is not None
        provider = self._providers[state.logical_model]
        state.pre_upstream_ms = (time.perf_counter() - state.started_perf) * 1000

        async def open_first():
            upstream = await provider.open_stream(unified)
            try:
                async for event in upstream:
                    if isinstance(event, TextDelta) and event.text:
                        return upstream, event
                    if isinstance(event, DoneEvent):
                        raise GatewayError("upstream_empty_response", "上游流没有返回任何文本")
                raise GatewayError("stream_interrupted", "上游流在提交点前结束")
            except BaseException:
                await upstream.aclose()
                raise

        result = await self._retrier.run(
            open_first,
            deadline=deadline,
            on_attempt=lambda record: self._on_attempt(state, record),
        )
        upstream, first = result.value
        state.upstream_model = state.spec.upstream_model
        session = StreamSession(
            state=state,
            upstream=upstream,
            first_event=first,
            heartbeat_interval_s=self._settings.stream.heartbeat_interval_s,
            finalize=lambda status, error: self._finalize(state, status, error),
            build_gateway=lambda latency: self._gateway_meta(state, latency),
            bus=self._bus,
        )
        return ChatResult(stream=session)

    # ------------------------------------------------------------------ 事件与元数据

    def _gateway_meta(self, state: _RunState, latency_ms: float) -> dict[str, Any]:
        prompt = None
        if state.prompt_name is not None:
            prompt = {"name": state.prompt_name, "version": state.prompt_version}
        return {
            "request_id": state.request_id,
            "upstream_model": state.upstream_model
            or (state.spec.upstream_model if state.spec else None),
            "protocol": state.spec.protocol if state.spec else None,
            "retry_count": state.retry_count,
            "repair_count": state.repair_count,
            "latency_ms": round(latency_ms, 1),
            "prompt": prompt,
        }

    def _finalize(self, state: _RunState, status: str, error: GatewayError | None) -> None:
        if state.finalized:
            return
        state.finalized = True
        latency_ms = (time.perf_counter() - state.started_perf) * 1000
        ttft = state.ttft_ms
        generation_ms = None
        if state.stream and ttft is not None:
            generation_ms = max(0.0, latency_ms - ttft)
        self._bus.emit(
            RequestFinished(
                request_id=state.request_id,
                ts_start=state.ts_start,
                status=status,
                logical_model=state.logical_model,
                upstream_model=state.upstream_model
                or (state.spec.upstream_model if state.spec else None),
                protocol=state.spec.protocol if state.spec else None,
                stream=state.stream,
                finish_reason=state.finish_reason,
                error_code=error.code if error else None,
                error_stage=error.stage if error else None,
                retry_count=state.retry_count,
                repair_count=state.repair_count,
                attempts=[attempt.as_dict() for attempt in state.attempts],
                usage=state.usage,
                output_chars=state.output_chars,
                emitted_chars=state.emitted_chars,
                latency_total_ms=round(latency_ms, 1),
                ttft_ms=round(ttft, 1) if ttft is not None else None,
                generation_ms=round(generation_ms, 1) if generation_ms is not None else None,
                pre_upstream_ms=round(state.pre_upstream_ms, 1),
                retry_wait_ms=round(state.retry_wait_ms, 1),
                prompt_name=state.prompt_name,
                prompt_version=state.prompt_version,
                prompt_fingerprint=state.prompt_fingerprint,
                prompt_chars=state.prompt_chars,
            )
        )
