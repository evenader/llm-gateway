"""结构化输出：Schema 自校验、本地三层校验、提示词注入与修复指令。"""
from __future__ import annotations

import json
from typing import Any, Callable, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from gateway.core.errors import GatewayError, sanitize
from gateway.core.types import ResponseFormat

BusinessValidator = Callable[[Any, ResponseFormat], Sequence[dict[str, Any]]]


def validate_response_format_schema(fmt: ResponseFormat) -> None:
    if fmt.type != "json_schema" or fmt.json_schema is None:
        return
    try:
        Draft202012Validator.check_schema(fmt.json_schema.schema)
    except SchemaError as exc:
        raise GatewayError(
            "invalid_response_format",
            f"请求中的 JSON Schema 非法: {exc.message}",
            details={"reason": sanitize(exc.message)},
        ) from exc


def _json_path(path: Any) -> str:
    out = "$"
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}"
    return out


def validate_output(
    text: str,
    fmt: ResponseFormat,
    business_validators: Sequence[BusinessValidator] = (),
) -> tuple[Any | None, list[dict[str, Any]]]:
    """返回 (parsed, errors)。任何一层失败都返回非空 errors，不做静默修正。"""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        return None, [{"path": "$", "type": "json_syntax", "message": sanitize(str(exc), 200)}]

    if fmt.type == "json_object":
        if not isinstance(parsed, dict):
            return parsed, [
                {
                    "path": "$",
                    "type": "type",
                    "expected": "object",
                    "actual": type(parsed).__name__,
                }
            ]
        return parsed, []

    if fmt.type != "json_schema" or fmt.json_schema is None:
        return parsed, []

    validator = Draft202012Validator(fmt.json_schema.schema)
    errors: list[dict[str, Any]] = []
    for error in validator.iter_errors(parsed):
        errors.append(
            {
                "path": _json_path(error.path),
                "type": error.validator or "schema",
                "message": sanitize(error.message, 200),
            }
        )
    errors.sort(key=lambda item: item["path"])

    for validator_fn in business_validators:
        extra = list(validator_fn(parsed, fmt))
        errors.extend(extra)
    return parsed, errors


def build_structured_instruction(fmt: ResponseFormat) -> str:
    if fmt.type == "json_object":
        return "只输出一个合法的 JSON 对象，不要代码围栏，不要附加说明。"
    assert fmt.json_schema is not None
    schema_json = json.dumps(fmt.json_schema.schema, ensure_ascii=False, separators=(",", ":"))
    return (
        "只输出符合以下 JSON Schema 的合法 JSON，不要代码围栏、不要附加说明。\n"
        f"Schema: {schema_json}"
    )


def build_repair_instruction(errors: list[dict[str, Any]]) -> str:
    parts = [f"{item.get('path', '$')}: {item.get('type', 'invalid')}" for item in errors[:10]]
    return (
        "上一次输出未通过 JSON 校验。错误："
        + "；".join(parts)
        + "。只修正出错的语法和字段，其余保持不变，只输出 JSON，不要代码围栏。"
    )
