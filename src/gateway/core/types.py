"""内部统一类型：适配器与 service 之间只使用这些类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]
FinishReason = Literal["stop", "length", "content_filter", "error"]


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class JsonSchemaSpec:
    name: str
    schema: dict[str, Any]
    strict: bool = True


@dataclass(frozen=True)
class ResponseFormat:
    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: JsonSchemaSpec | None = None

    @property
    def is_structured(self) -> bool:
        return self.type in ("json_object", "json_schema")


@dataclass(frozen=True)
class UnifiedRequest:
    messages: tuple[Message, ...]
    upstream_model: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: tuple[str, ...] = ()
    response_format: ResponseFormat = field(default_factory=ResponseFormat)
    stream: bool = False
    thinking: str = "disabled"


@dataclass
class Usage:
    """归一化用量。input_tokens 已包含缓存命中部分；缺失字段为 None，不估算。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    usage_missing: bool = False

    def add(self, other: "Usage") -> "Usage":
        def _sum(left: int | None, right: int | None) -> int | None:
            if left is None and right is None:
                return None
            return (left or 0) + (right or 0)

        return Usage(
            input_tokens=_sum(self.input_tokens, other.input_tokens),
            output_tokens=_sum(self.output_tokens, other.output_tokens),
            cache_read_tokens=_sum(self.cache_read_tokens, other.cache_read_tokens),
            cache_creation_tokens=_sum(self.cache_creation_tokens, other.cache_creation_tokens),
            reasoning_tokens=_sum(self.reasoning_tokens, other.reasoning_tokens),
            total_tokens=_sum(self.total_tokens, other.total_tokens),
            usage_missing=self.usage_missing or other.usage_missing,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "prompt_tokens_details": {"cached_tokens": self.cache_read_tokens},
            "completion_tokens_details": {"reasoning_tokens": self.reasoning_tokens},
        }

    def trace_fields(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class UnifiedResponse:
    text: str
    finish_reason: FinishReason
    usage: Usage
    upstream_model: str


@dataclass(frozen=True)
class StartEvent:
    upstream_model: str | None = None


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class UsageEvent:
    usage: Usage


@dataclass(frozen=True)
class DoneEvent:
    finish_reason: FinishReason
    usage: Usage


StreamEvent = StartEvent | TextDelta | UsageEvent | DoneEvent
