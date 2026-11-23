"""对外请求 schema（pydantic v2，extra=forbid）。与内部类型完全分离。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gateway.core.types import JsonSchemaSpec, Message, ResponseFormat


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageIn(_Strict):
    role: Literal["system", "user", "assistant"]
    content: str


class JsonSchemaIn(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    schema_payload: dict[str, Any] = Field(alias="schema")
    strict: bool = True


class ResponseFormatIn(_Strict):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: JsonSchemaIn | None = None

    @model_validator(mode="after")
    def _check_schema_presence(self) -> "ResponseFormatIn":
        if self.type == "json_schema" and self.json_schema is None:
            raise ValueError("type=json_schema 时 json_schema 必填")
        if self.type != "json_schema" and self.json_schema is not None:
            raise ValueError("json_schema 只允许与 type=json_schema 同时使用")
        return self


class PromptRefIn(_Strict):
    name: str = Field(min_length=1)
    version: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(_Strict):
    model: str = Field(min_length=1)
    messages: list[MessageIn] | None = None
    prompt: PromptRefIn | None = None
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1)
    stop: list[str] | None = Field(default=None, max_length=16)
    response_format: ResponseFormatIn | None = None


def to_internal_response_format(fmt: ResponseFormatIn | None) -> ResponseFormat:
    if fmt is None or fmt.type == "text":
        return ResponseFormat(type="text")
    if fmt.type == "json_object":
        return ResponseFormat(type="json_object")
    assert fmt.json_schema is not None
    return ResponseFormat(
        type="json_schema",
        json_schema=JsonSchemaSpec(
            name=fmt.json_schema.name,
            schema=fmt.json_schema.schema_payload,
            strict=fmt.json_schema.strict,
        ),
    )


def to_internal_messages(messages: list[MessageIn]) -> list[Message]:
    return [Message(role=message.role, content=message.content) for message in messages]


def validation_fields(exc: Any) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    for error in exc.errors():
        location = error.get("loc", ())
        path = ".".join(str(part) for part in location) or "$"
        fields.append(
            {
                "path": path,
                "type": str(error.get("type", "invalid")),
                "message": str(error.get("msg", "invalid")),
            }
        )
    return fields
