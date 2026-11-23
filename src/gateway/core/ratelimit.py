"""按逻辑模型独立的令牌桶限流（进程内、惰性补充、同步非阻塞）。"""
from __future__ import annotations

import math

from gateway.core.clock import Clock


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int, clock: Clock) -> None:
        self.rate_per_s = float(rate_per_s)
        self.burst = int(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock.monotonic()

    def _refill(self) -> None:
        now = self._clock.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate_per_s)
            self._last = now
        elif elapsed < 0:  # 时钟回拨时只更新基准，不加令牌
            self._last = now

    def try_acquire(self, tokens: int = 1) -> tuple[bool, float]:
        """返回 (是否拿到, 不足时建议等待秒数)。不含 await，事件循环内天然原子。"""
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True, 0.0
        missing = tokens - self._tokens
        retry_after = 1.0
        if self.rate_per_s > 0:
            retry_after = float(max(1, math.ceil(missing / self.rate_per_s)))
        return False, retry_after

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens


class RateLimiter:
    def __init__(self, *, default_rate_per_s: float, default_burst: int, clock: Clock) -> None:
        self._default_rate = float(default_rate_per_s)
        self._default_burst = int(default_burst)
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(self, model: str, rate_per_s: float | None, burst: int | None) -> TokenBucket:
        existing = self._buckets.get(model)
        if existing is not None:
            return existing
        bucket = TokenBucket(
            rate_per_s=self._default_rate if rate_per_s is None else rate_per_s,
            burst=self._default_burst if burst is None else burst,
            clock=self._clock,
        )
        self._buckets[model] = bucket
        return bucket
