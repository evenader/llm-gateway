from __future__ import annotations

import pytest

from gateway.core.errors import GatewayError
from gateway.core.types import JsonSchemaSpec, ResponseFormat
from gateway.services.structured import (
    build_repair_instruction,
    build_structured_instruction,
    validate_output,
    validate_response_format_schema,
)


def test_json_object_requires_object() -> None:
    parsed, errors = validate_output("[1, 2]", ResponseFormat(type="json_object"))
    assert parsed == [1, 2]
    assert errors and errors[0]["type"] == "type"


def test_json_object_ok() -> None:
    parsed, errors = validate_output('{"a": 1}', ResponseFormat(type="json_object"))
    assert parsed == {"a": 1}
    assert errors == []


def test_json_syntax_error_is_not_silently_fixed() -> None:
    parsed, errors = validate_output('```json\n{"a": 1}\n```', ResponseFormat(type="json_object"))
    assert parsed is None
    assert errors[0]["type"] == "json_syntax"


def test_json_schema_collects_paths() -> None:
    fmt = ResponseFormat(
        type="json_schema",
        json_schema=JsonSchemaSpec(
            name="person",
            schema={
                "type": "object",
                "required": ["name", "age"],
                "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            },
        ),
    )
    _, errors = validate_output('{"age": "x"}', fmt)
    paths = {item["path"] for item in errors}
    assert "$.age" in paths
    assert any(item["type"] == "required" for item in errors)


def test_invalid_schema_rejected() -> None:
    fmt = ResponseFormat(
        type="json_schema", json_schema=JsonSchemaSpec(name="bad", schema={"type": 5})
    )
    with pytest.raises(GatewayError) as excinfo:
        validate_response_format_schema(fmt)
    assert excinfo.value.code == "invalid_response_format"


def test_instruction_contains_json_and_schema() -> None:
    fmt = ResponseFormat(
        type="json_schema",
        json_schema=JsonSchemaSpec(name="x", schema={"type": "object", "properties": {}}),
    )
    instruction = build_structured_instruction(fmt)
    assert "JSON" in instruction
    assert '"type":"object"' in instruction


def test_repair_instruction_has_paths_and_types_only() -> None:
    text = build_repair_instruction([{"path": "$.a", "type": "required"}, {"path": "$", "type": "json_syntax"}])
    assert "$.a: required" in text
    assert "json_syntax" in text
    assert "只输出 JSON" in text
    assert "网关" not in text
