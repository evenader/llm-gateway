"""Trace 存储：WAL SQLite，后台线程批量写入，查询与按模型聚合。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    request_id TEXT PRIMARY KEY,
    ts_start REAL NOT NULL,
    ts_start_iso TEXT NOT NULL,
    logical_model TEXT,
    upstream_model TEXT,
    protocol TEXT,
    stream INTEGER NOT NULL DEFAULT 0,
    prompt_name TEXT,
    prompt_version TEXT,
    prompt_fingerprint TEXT,
    prompt_chars INTEGER,
    output_chars INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    finish_reason TEXT,
    error_code TEXT,
    error_stage TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    repair_count INTEGER NOT NULL DEFAULT 0,
    attempts TEXT NOT NULL DEFAULT '[]',
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    usage_missing INTEGER NOT NULL DEFAULT 0,
    latency_total_ms REAL,
    ttft_ms REAL,
    generation_ms REAL,
    pre_upstream_ms REAL,
    retry_wait_ms REAL,
    emitted_chars INTEGER NOT NULL DEFAULT 0,
    cost_estimate REAL
)
"""

_COLUMNS = (
    "request_id, ts_start, ts_start_iso, logical_model, upstream_model, protocol, stream,"
    " prompt_name, prompt_version, prompt_fingerprint, prompt_chars, output_chars, status,"
    " finish_reason, error_code, error_stage, retry_count, repair_count, attempts,"
    " input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, reasoning_tokens,"
    " total_tokens, usage_missing, latency_total_ms, ttft_ms, generation_ms, pre_upstream_ms,"
    " retry_wait_ms, emitted_chars, cost_estimate"
)


def _parse_since(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 1)


def _sum(values: list[int | None]) -> int:
    return sum(value for value in values if value is not None)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get("logical_model") or "unknown", []).append(row)
    models: dict[str, Any] = {}
    for model, items in sorted(groups.items()):
        total = len(items)
        success = sum(1 for item in items if item["status"] == "completed")
        latencies = [item["latency_total_ms"] for item in items if item.get("latency_total_ms") is not None]
        ttfts = [item["ttft_ms"] for item in items if item.get("ttft_ms") is not None]
        error_counts: dict[str, int] = {}
        for item in items:
            if item.get("error_code"):
                error_counts[item["error_code"]] = error_counts.get(item["error_code"], 0) + 1
        models[model] = {
            "requests": total,
            "success": success,
            "success_rate": round(success / total, 4) if total else 0.0,
            "input_tokens": _sum([item.get("input_tokens") for item in items]),
            "output_tokens": _sum([item.get("output_tokens") for item in items]),
            "cache_read_tokens": _sum([item.get("cache_read_tokens") for item in items]),
            "cache_creation_tokens": _sum([item.get("cache_creation_tokens") for item in items]),
            "reasoning_tokens": _sum([item.get("reasoning_tokens") for item in items]),
            "total_tokens": _sum([item.get("total_tokens") for item in items]),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "p50_latency_ms": _percentile(latencies, 0.5),
            "p95_latency_ms": _percentile(latencies, 0.95),
            "avg_ttft_ms": round(sum(ttfts) / len(ttfts), 1) if ttfts else None,
            "p50_ttft_ms": _percentile(ttfts, 0.5),
            "p95_ttft_ms": _percentile(ttfts, 0.95),
            "retry_total": sum(int(item.get("retry_count") or 0) for item in items),
            "error_counts": error_counts,
        }
    return {"requests": len(rows), "models": models}


class TraceStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def write_batch(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        placeholders = ", ".join("?" for _ in range(len(_COLUMNS.split(","))))
        sql = f"INSERT OR REPLACE INTO traces ({_COLUMNS}) VALUES ({placeholders})"
        conn = self._connect()
        try:
            with conn:
                conn.executemany(sql, [tuple(row[column.strip()] for column in _COLUMNS.split(",")) for row in rows])
        finally:
            conn.close()

    def query(
        self,
        *,
        model: str | None = None,
        status: str | None = None,
        since: Any = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if model:
            clauses.append("logical_model = ?")
            params.append(model)
        if status:
            clauses.append("status = ?")
            params.append(status)
        since_value = _parse_since(since)
        if since_value is not None:
            clauses.append("ts_start >= ?")
            params.append(since_value)
        sql = "SELECT * FROM traces"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts_start DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [self._row_to_dict(row) for row in rows]

    def get(self, request_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM traces WHERE request_id = ?", (request_id,)).fetchone()
        finally:
            conn.close()
        return self._row_to_dict(row) if row else None

    def stats(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            rows = [self._row_to_dict(row) for row in conn.execute("SELECT * FROM traces").fetchall()]
        finally:
            conn.close()
        return summarize(rows)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["stream"] = bool(data.get("stream"))
        data["usage_missing"] = bool(data.get("usage_missing"))
        try:
            data["attempts"] = json.loads(data.get("attempts") or "[]")
        except ValueError:
            data["attempts"] = []
        return data
