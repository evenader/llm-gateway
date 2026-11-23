from __future__ import annotations

import random

import pytest

from gateway.core.clock import SystemClock
from gateway.core.errors import GatewayError
from gateway.core.retry import Retrier, RetryPolicy


class AdvancingClock(SystemClock):
    def __init__(self) -> None:
        self._value = 1000.0

    def monotonic(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        self._value += seconds


def make_retrier(policy: RetryPolicy, clock: AdvancingClock):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.advance(delay)

    return Retrier(policy, sleep=fake_sleep, clock=clock), sleeps


async def test_backoff_sequence_no_jitter() -> None:
    clock = AdvancingClock()
    retrier, sleeps = make_retrier(
        RetryPolicy(max_retries=3, base_delay_s=0.5, factor=2.0, max_delay_s=8.0, jitter="none"), clock
    )

    async def fail():
        raise GatewayError("upstream_overloaded", "503")

    with pytest.raises(GatewayError):
        await retrier.run(fail)
    assert sleeps == [0.5, 1.0, 2.0]


async def test_max_delay_cap() -> None:
    clock = AdvancingClock()
    retrier, sleeps = make_retrier(
        RetryPolicy(max_retries=3, base_delay_s=3.0, factor=4.0, max_delay_s=8.0, jitter="none"), clock
    )

    async def fail():
        raise GatewayError("upstream_overloaded", "503")

    with pytest.raises(GatewayError):
        await retrier.run(fail)
    assert sleeps == [3.0, 8.0, 8.0]


async def test_non_retryable_stops_immediately() -> None:
    clock = AdvancingClock()
    retrier, sleeps = make_retrier(RetryPolicy(jitter="none"), clock)

    async def fail():
        raise GatewayError("upstream_bad_request", "400")

    with pytest.raises(GatewayError):
        await retrier.run(fail)
    assert sleeps == []


async def test_retry_after_extends_delay() -> None:
    clock = AdvancingClock()
    retrier, sleeps = make_retrier(
        RetryPolicy(max_retries=1, base_delay_s=0.5, factor=2.0, max_delay_s=8.0, jitter="none"), clock
    )

    async def fail():
        raise GatewayError("upstream_rate_limited", "429", details={"retry_after_s": 2.0})

    with pytest.raises(GatewayError):
        await retrier.run(fail)
    assert sleeps == [2.0]


async def test_retry_after_over_max_delay_no_retry() -> None:
    clock = AdvancingClock()
    retrier, sleeps = make_retrier(
        RetryPolicy(max_retries=3, base_delay_s=0.5, max_delay_s=8.0, jitter="none"), clock
    )

    async def fail():
        raise GatewayError("upstream_rate_limited", "429", details={"retry_after_s": 100.0})

    with pytest.raises(GatewayError):
        await retrier.run(fail)
    assert sleeps == []


async def test_deadline_exhaustion() -> None:
    clock = AdvancingClock()
    retrier, sleeps = make_retrier(
        RetryPolicy(max_retries=3, base_delay_s=0.5, factor=2.0, max_delay_s=8.0, jitter="none"), clock
    )
    deadline = clock.monotonic() + 0.1

    async def fail():
        raise GatewayError("upstream_overloaded", "503")

    with pytest.raises(GatewayError):
        await retrier.run(fail, deadline=deadline)
    assert sleeps == []


async def test_success_records_attempts_and_retry_count() -> None:
    clock = AdvancingClock()
    retrier, sleeps = make_retrier(
        RetryPolicy(max_retries=2, base_delay_s=1.0, factor=2.0, max_delay_s=8.0, jitter="none"), clock
    )
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise GatewayError("upstream_overloaded", "503")
        return "ok"

    result = await retrier.run(flaky)
    assert result.value == "ok"
    assert result.retry_count == 2
    assert [attempt.error_code for attempt in result.attempts] == [
        "upstream_overloaded",
        "upstream_overloaded",
        None,
    ]
    assert sleeps == [1.0, 2.0]


def test_full_jitter_within_bounds() -> None:
    clock = AdvancingClock()
    retrier, _ = make_retrier(
        RetryPolicy(max_retries=3, base_delay_s=0.5, factor=2.0, max_delay_s=8.0, jitter="full"), clock
    )
    retrier._rng = random.Random(3)  # noqa: SLF001
    assert 0 <= retrier.delay_for(1) <= 0.5
    assert 0 <= retrier.delay_for(2) <= 1.0
    assert 0 <= retrier.delay_for(3) <= 2.0
