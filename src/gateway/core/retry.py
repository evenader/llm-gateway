"""指数退避重试：sleep / rng / clock 全部注入，便于用假时钟验证且不真实等待。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Literal, TypeVar

from gateway.core.clock import Clock
from gateway.core.errors import GatewayError

T = TypeVar("T")

Jitter = Literal["none", "full", "equal"]


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_s: float = 0.5
    factor: float = 2.0
    max_delay_s: float = 8.0
    jitter: Jitter = "full"


@dataclass(frozen=True)
class AttemptRecord:
    index: int
    duration_ms: float
    error_code: str | None = None
    backoff_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "backoff_ms": self.backoff_ms,
        }


@dataclass
class RetryResult(Generic[T]):
    value: T
    attempts: list[AttemptRecord]
    retry_count: int
    retry_wait_ms: float


OnAttempt = Callable[[AttemptRecord], None]


class Retrier:
    def __init__(
        self,
        policy: RetryPolicy,
        *,
        sleep: Callable[[float], Awaitable[None]],
        clock: Clock,
        rng: Any | None = None,
    ) -> None:
        import random

        self.policy = policy
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()

    def delay_for(self, retry_index: int) -> float:
        """retry_index 从 1 开始，表示第几次重试。"""
        if retry_index < 1:
            raise ValueError("retry_index must be >= 1")
        raw = min(
            self.policy.max_delay_s,
            self.policy.base_delay_s * (self.policy.factor ** (retry_index - 1)),
        )
        if self.policy.jitter == "none":
            return raw
        if self.policy.jitter == "full":
            return self._rng.uniform(0.0, raw)
        return raw / 2 + self._rng.uniform(0.0, raw / 2)

    async def run(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        deadline: float | None = None,
        on_attempt: OnAttempt | None = None,
    ) -> RetryResult[T]:
        attempts: list[AttemptRecord] = []
        retries = 0
        wait_ms = 0.0

        while True:
            index = len(attempts) + 1
            started = self._clock.monotonic()
            try:
                value = await fn()
            except GatewayError as exc:
                duration_ms = (self._clock.monotonic() - started) * 1000
                can_retry = exc.retryable and retries < self.policy.max_retries
                delay = 0.0
                if can_retry:
                    delay = self.delay_for(retries + 1)
                    retry_after = exc.retry_after_s()
                    if retry_after is not None:
                        if retry_after > self.policy.max_delay_s:
                            can_retry = False
                        else:
                            delay = max(delay, retry_after)
                if can_retry and deadline is not None:
                    if self._clock.monotonic() + delay >= deadline:
                        can_retry = False
                record = AttemptRecord(
                    index=index,
                    duration_ms=round(duration_ms, 3),
                    error_code=exc.code,
                    backoff_ms=round(delay * 1000, 3) if can_retry else 0.0,
                )
                attempts.append(record)
                if on_attempt is not None:
                    on_attempt(record)
                if not can_retry:
                    raise
                await self._sleep(delay)
                retries += 1
                wait_ms += delay * 1000
                continue

            duration_ms = (self._clock.monotonic() - started) * 1000
            record = AttemptRecord(index=index, duration_ms=round(duration_ms, 3), error_code=None)
            attempts.append(record)
            if on_attempt is not None:
                on_attempt(record)
            return RetryResult(value=value, attempts=attempts, retry_count=retries, retry_wait_ms=wait_ms)


def compute_backoff_sequence(policy: RetryPolicy, count: int) -> list[float]:
    """验证辅助：按公式给出无 jitter 的退避序列。"""
    return [
        min(policy.max_delay_s, policy.base_delay_s * math.pow(policy.factor, n - 1))
        for n in range(1, count + 1)
    ]
