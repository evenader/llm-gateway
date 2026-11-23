"""生命周期事件总线。core 只发事件，不知道订阅者；observability 通过订阅旁路接入。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from gateway.core.types import Usage


@dataclass(frozen=True)
class RequestStarted:
    request_id: str
    ts_start: float


@dataclass(frozen=True)
class AttemptFinished:
    request_id: str
    attempt: dict[str, Any]


@dataclass(frozen=True)
class FirstTokenReceived:
    request_id: str
    ttft_ms: float


@dataclass(frozen=True)
class RequestFinished:
    """唯一终态事件，携带完整汇总。"""

    request_id: str
    ts_start: float
    status: str
    logical_model: str | None
    upstream_model: str | None
    protocol: str | None
    stream: bool
    finish_reason: str | None
    error_code: str | None
    error_stage: str | None
    retry_count: int
    repair_count: int
    attempts: list[dict[str, Any]]
    usage: Usage | None
    output_chars: int
    emitted_chars: int
    latency_total_ms: float
    ttft_ms: float | None
    generation_ms: float | None
    pre_upstream_ms: float
    retry_wait_ms: float
    prompt_name: str | None = None
    prompt_version: str | None = None
    prompt_fingerprint: str | None = None
    prompt_chars: int | None = None


EventHandler = Callable[[object], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self.subscriber_errors = 0

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def emit(self, event: object) -> None:
        for handler in list(self._handlers):
            try:
                handler(event)
            except Exception:  # 订阅者异常不得影响请求
                self.subscriber_errors += 1
