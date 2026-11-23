from __future__ import annotations

from gateway.core.clock import OffsetClock
from gateway.core.ratelimit import RateLimiter, TokenBucket


def test_burst_and_refill() -> None:
    clock = OffsetClock()
    bucket = TokenBucket(rate_per_s=1.0, burst=3, clock=clock)
    assert [bucket.try_acquire()[0] for _ in range(3)] == [True, True, True]
    acquired, retry_after = bucket.try_acquire()
    assert acquired is False
    assert retry_after >= 1
    clock.advance(1.0)
    assert bucket.try_acquire()[0] is True


def test_refill_is_capped_at_burst() -> None:
    clock = OffsetClock()
    bucket = TokenBucket(rate_per_s=10.0, burst=2, clock=clock)
    clock.advance(100.0)
    assert bucket.try_acquire()[0] is True
    assert bucket.try_acquire()[0] is True
    assert bucket.try_acquire()[0] is False


def test_models_are_independent() -> None:
    clock = OffsetClock()
    limiter = RateLimiter(default_rate_per_s=1.0, default_burst=2, clock=clock)
    alpha = limiter.bucket("alpha", 1.0, 2)
    beta = limiter.bucket("beta", 1.0, 2)
    assert alpha.try_acquire()[0] is True
    assert alpha.try_acquire()[0] is True
    assert alpha.try_acquire()[0] is False
    assert beta.try_acquire()[0] is True


def test_default_rate_limit_used_when_model_has_none() -> None:
    clock = OffsetClock()
    limiter = RateLimiter(default_rate_per_s=2.0, default_burst=1, clock=clock)
    bucket = limiter.bucket("m", None, None)
    assert bucket.rate_per_s == 2.0
    assert bucket.burst == 1
