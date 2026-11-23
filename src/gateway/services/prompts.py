"""提示词模板与版本管理：独立 SQLite 存储、SemVer 解析、单次扫描渲染、静态校验。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from gateway.config import ConfigError, PromptsConfig
from gateway.core.errors import GatewayError

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
ROLES = {"system", "user", "assistant"}
DEFAULT_MAX_LENGTH = 20000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    changelog TEXT NOT NULL DEFAULT '',
    messages TEXT NOT NULL,
    variables TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
)
"""


def semver_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def canonical_hash(messages: list[dict[str, Any]], variables: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        {"messages": messages, "variables": variables},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def messages_fingerprint(messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptRecord:
    name: str
    version: str
    description: str
    owner: str
    changelog: str
    messages: list[dict[str, str]]
    variables: list[dict[str, Any]]
    content_hash: str
    created_at: str

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "owner": self.owner,
            "changelog": self.changelog,
            "messages": self.messages,
            "variables": self.variables,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RenderedPrompt:
    name: str
    version: str
    messages: list[dict[str, str]]
    fingerprint: str
    chars: int


def _render_error(message: str, *, fields: list[dict[str, str]] | None = None) -> GatewayError:
    return GatewayError(
        "template_render_error",
        message,
        details={"fields": fields} if fields else None,
    )


class PromptStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def insert(self, record: PromptRecord) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO prompts(name, version, description, owner, changelog, messages, variables, content_hash, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.name,
                        record.version,
                        record.description,
                        record.owner,
                        record.changelog,
                        json.dumps(record.messages, ensure_ascii=False),
                        json.dumps(record.variables, ensure_ascii=False),
                        record.content_hash,
                        record.created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise GatewayError(
                "template_conflict",
                f"模板 {record.name}@{record.version} 已存在",
                details={"name": record.name, "version": record.version},
            ) from exc

    def get_exact(self, name: str, version: str) -> PromptRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM prompts WHERE name = ? AND version = ?", (name, version)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def versions(self, name: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT version FROM prompts WHERE name = ?", (name,)).fetchall()
        return sorted((row["version"] for row in rows), key=semver_tuple)

    def list_names(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT name FROM prompts ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> PromptRecord:
        return PromptRecord(
            name=row["name"],
            version=row["version"],
            description=row["description"],
            owner=row["owner"],
            changelog=row["changelog"],
            messages=json.loads(row["messages"]),
            variables=json.loads(row["variables"]),
            content_hash=row["content_hash"],
            created_at=row["created_at"],
        )


class PromptService:
    def __init__(self, config: PromptsConfig, *, store: PromptStore | None = None) -> None:
        self.config = config
        self.store = store or PromptStore(config.db_path)

    # ------------------------------------------------------------------ 种子

    def seed_from_dir(self) -> None:
        directory = Path(self.config.seed_dir)
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ConfigError(f"种子文件 {path.name} 格式非法")
                record = self._normalize(entry)
                existing = self.store.get_exact(record.name, record.version)
                if existing is None:
                    self.store.insert(record)
                elif existing.content_hash != record.content_hash:
                    raise ConfigError(
                        f"种子冲突：{record.name}@{record.version} 已存在且内容不同，拒绝覆盖"
                    )

    # ------------------------------------------------------------------ 创建与读取

    def _normalize(self, payload: dict[str, Any]) -> PromptRecord:
        name = payload.get("name")
        version = payload.get("version")
        if not isinstance(name, str) or not name.strip():
            raise _render_error("模板 name 必须是非空字符串", fields=[{"path": "name"}])
        if not isinstance(version, str) or not SEMVER.match(version):
            raise _render_error("模板 version 必须是 SemVer X.Y.Z", fields=[{"path": "version"}])

        messages_raw = payload.get("messages")
        if not isinstance(messages_raw, list) or not messages_raw:
            raise _render_error("messages 必须是非空列表", fields=[{"path": "messages"}])
        messages: list[dict[str, str]] = []
        for index, item in enumerate(messages_raw):
            if not isinstance(item, dict):
                raise _render_error("message 必须是对象", fields=[{"path": f"messages.{index}"}])
            role = item.get("role")
            content = item.get("content")
            if role not in ROLES:
                raise _render_error(
                    f"非法角色: {role}", fields=[{"path": f"messages.{index}.role"}]
                )
            if not isinstance(content, str):
                raise _render_error(
                    "content 必须是字符串", fields=[{"path": f"messages.{index}.content"}]
                )
            messages.append({"role": role, "content": content})

        variables_raw = payload.get("variables") or []
        if not isinstance(variables_raw, list):
            raise _render_error("variables 必须是列表", fields=[{"path": "variables"}])
        variables: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(variables_raw):
            if not isinstance(item, dict):
                raise _render_error("变量必须是对象", fields=[{"path": f"variables.{index}"}])
            var_name = item.get("name")
            if not isinstance(var_name, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", var_name):
                raise _render_error(
                    "变量名非法", fields=[{"path": f"variables.{index}.name"}]
                )
            if var_name in seen:
                raise _render_error(
                    "变量名重复", fields=[{"path": f"variables.{index}.name"}]
                )
            seen.add(var_name)
            required = item.get("required", True)
            if not isinstance(required, bool):
                raise _render_error(
                    "required 必须是布尔值", fields=[{"path": f"variables.{index}.required"}]
                )
            default = item.get("default")
            if default is not None and not isinstance(default, (str, int, float, bool)):
                raise _render_error(
                    "default 只能是 str/int/float/bool",
                    fields=[{"path": f"variables.{index}.default"}],
                )
            max_length = item.get("max_length", DEFAULT_MAX_LENGTH)
            if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length <= 0:
                raise _render_error(
                    "max_length 必须是正整数", fields=[{"path": f"variables.{index}.max_length"}]
                )
            variables.append(
                {
                    "name": var_name,
                    "required": required,
                    "default": default,
                    "max_length": max_length,
                }
            )

        used: set[str] = set()
        for message in messages:
            for match in PLACEHOLDER.finditer(message["content"]):
                used.add(match.group(1))
        declared = {item["name"] for item in variables}
        if used - declared:
            raise _render_error(
                "存在未声明的占位符: " + ", ".join(sorted(used - declared)),
                fields=[{"path": "messages"}],
            )
        if declared - used:
            raise _render_error(
                "存在声明但未使用的变量: " + ", ".join(sorted(declared - used)),
                fields=[{"path": "variables"}],
            )

        created_at = datetime.now(timezone.utc).isoformat()
        return PromptRecord(
            name=name,
            version=version,
            description=str(payload.get("description") or ""),
            owner=str(payload.get("owner") or ""),
            changelog=str(payload.get("changelog") or ""),
            messages=messages,
            variables=variables,
            content_hash=canonical_hash(messages, variables),
            created_at=created_at,
        )

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self._normalize(payload)
        self.store.insert(record)
        return record.public()

    def resolve(self, name: str, version: str | None) -> PromptRecord:
        if version is None or version == "latest":
            versions = self.store.versions(name)
            if not versions:
                raise GatewayError(
                    "template_not_found",
                    f"模板 {name} 不存在",
                    details={"name": name, "version": version},
                )
            record = self.store.get_exact(name, versions[-1])
        else:
            record = self.store.get_exact(name, version)
            if record is None:
                raise GatewayError(
                    "template_not_found",
                    f"模板 {name}@{version} 不存在",
                    details={"name": name, "version": version},
                )
        assert record is not None
        return record

    def get_public(self, name: str, version: str | None) -> dict[str, Any]:
        return self.resolve(name, version).public()

    def list_templates(self) -> list[dict[str, Any]]:
        result = []
        for name in self.store.list_names():
            versions = self.store.versions(name)
            result.append(
                {
                    "name": name,
                    "versions": versions,
                    "latest_version": versions[-1] if versions else None,
                }
            )
        return result

    # ------------------------------------------------------------------ 渲染

    def render(
        self, name: str, version: str | None, variables: dict[str, Any] | None
    ) -> RenderedPrompt:
        record = self.resolve(name, version)
        provided = variables or {}
        if not isinstance(provided, dict):
            raise _render_error("variables 必须是对象", fields=[{"path": "prompt.variables"}])

        declared = {item["name"]: item for item in record.variables}
        extra = set(provided) - set(declared)
        if extra:
            raise _render_error(
                "存在未声明的变量: " + ", ".join(sorted(extra)),
                fields=[{"path": f"prompt.variables.{item}"} for item in sorted(extra)],
            )

        resolved: dict[str, str] = {}
        for var_name, spec in declared.items():
            if var_name in provided:
                value = provided[var_name]
            elif spec.get("default") is not None:
                value = spec["default"]
            elif spec.get("required", True):
                raise GatewayError(
                    "template_variable_missing",
                    f"缺少必填变量: {var_name}",
                    details={"variable": var_name, "name": name, "version": record.version},
                )
            else:
                value = ""

            if not isinstance(value, (str, int, float, bool)) or value is None:
                raise _render_error(
                    f"变量 {var_name} 类型非法（只允许 str/int/float/bool）",
                    fields=[{"path": f"prompt.variables.{var_name}"}],
                )
            text = str(value)
            max_length = int(spec.get("max_length", DEFAULT_MAX_LENGTH))
            if len(text) > max_length:
                raise _render_error(
                    f"变量 {var_name} 超长（{len(text)} > {max_length}）",
                    fields=[{"path": f"prompt.variables.{var_name}"}],
                )
            resolved[var_name] = text

        def replace(match: re.Match[str]) -> str:
            return resolved.get(match.group(1), match.group(0))

        rendered_messages = [
            {
                "role": message["role"],
                "content": PLACEHOLDER.sub(replace, message["content"]),
            }
            for message in record.messages
        ]
        chars = sum(len(message["content"]) for message in rendered_messages)
        return RenderedPrompt(
            name=record.name,
            version=record.version,
            messages=rendered_messages,
            fingerprint=messages_fingerprint(rendered_messages),
            chars=chars,
        )
