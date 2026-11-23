from __future__ import annotations

from gateway.core.types import Usage
from gateway.providers.anthropic_messages import map_usage as anthropic_usage
from gateway.providers.openai_responses import map_usage as responses_usage


def test_responses_usage_normalization() -> None:
    usage = responses_usage(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens_details": {"reasoning_tokens": 20},
        }
    )
    assert usage.input_tokens == 100
    assert usage.cache_read_tokens == 30
    assert usage.output_tokens == 50
    assert usage.reasoning_tokens == 20
    assert usage.total_tokens == 150


def test_responses_usage_total_fallback() -> None:
    usage = responses_usage({"input_tokens": 7, "output_tokens": 3})
    assert usage.total_tokens == 10


def test_anthropic_usage_normalization() -> None:
    usage = anthropic_usage(
        {
            "input_tokens": 70,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 0,
            "output_tokens": 50,
        }
    )
    assert usage.input_tokens == 100
    assert usage.cache_read_tokens == 30
    assert usage.cache_creation_tokens == 0
    assert usage.output_tokens == 50
    assert usage.total_tokens == 150
    assert usage.reasoning_tokens is None


def test_missing_usage_is_flagged_not_estimated() -> None:
    assert responses_usage(None).usage_missing is True
    assert anthropic_usage({}).usage_missing is True
    assert responses_usage(None).input_tokens is None


def test_usage_add_accumulates_and_keeps_missing_flag() -> None:
    first = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
    second = Usage(input_tokens=20, output_tokens=7, total_tokens=27)
    total = first.add(second)
    assert (total.input_tokens, total.output_tokens, total.total_tokens) == (30, 12, 42)
    assert total.usage_missing is False

    missing = Usage(usage_missing=True)
    merged = first.add(missing)
    assert merged.usage_missing is True
    assert merged.input_tokens == 10
