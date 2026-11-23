"""Recorder：订阅事件 → 有界队列 → 后台线程批量写 SQLite；热路径不做磁盘 IO。"""
from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any

from gateway.core.events import RequestFinished
from gateway.observability.store import TraceStore

logger = logging.getLogger("gateway.observability")


class Recorder:
    def __init__(self, store: TraceStore, queue_size: int = 10000) -> None:
        self.store = store
        self.dropped = 0
        self.write_errors = 0
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_size))
        self._thread = threading.Thread(target=self._writer, name="trace-writer", daemon=True)
        self._thread.start()

    def __call__(self, event: object) -> None:
        if isinstance(event, RequestFinished):
            self._enqueue(self._build(event))

    def _enqueue(self, row: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    def _writer(self) -> None:
        while True:
            try:
                row = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if row is None:
                break
            batch = [row]
            while len(batch) < 100:
                try:
                    extra = self._queue.get_nowait()
                except queue.Empty:
                    break
                if extra is None:
                    try:
                        self.store.write_batch(batch)
                    except Exception:
                        self.write_errors += 1
                    return
                batch.append(extra)
            try:
                self.store.write_batch(batch)
            except Exception:
                self.write_errors += 1
                logger.error("trace write failed type=%s", "store_error")

    @staticmethod
    def _build(event: RequestFinished) -> dict[str, Any]:
        usage = event.usage
        attempts = json.dumps(event.attempts, ensure_ascii=False)
        return {
            "request_id": event.request_id,
            "ts_start": event.ts_start,
            "ts_start_iso": datetime.fromtimestamp(event.ts_start, tz=timezone.utc).isoformat(),
            "logical_model": event.logical_model,
            "upstream_model": event.upstream_model,
            "protocol": event.protocol,
            "stream": 1 if event.stream else 0,
            "prompt_name": event.prompt_name,
            "prompt_version": event.prompt_version,
            "prompt_fingerprint": event.prompt_fingerprint,
            "prompt_chars": event.prompt_chars,
            "output_chars": event.output_chars,
            "status": event.status,
            "finish_reason": event.finish_reason,
            "error_code": event.error_code,
            "error_stage": event.error_stage,
            "retry_count": event.retry_count,
            "repair_count": event.repair_count,
            "attempts": attempts,
            "input_tokens": usage.input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "cache_read_tokens": usage.cache_read_tokens if usage else None,
            "cache_creation_tokens": usage.cache_creation_tokens if usage else None,
            "reasoning_tokens": usage.reasoning_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
            "usage_missing": 1 if (usage is None or usage.usage_missing) else 0,
            "latency_total_ms": event.latency_total_ms,
            "ttft_ms": event.ttft_ms,
            "generation_ms": event.generation_ms,
            "pre_upstream_ms": event.pre_upstream_ms,
            "retry_wait_ms": event.retry_wait_ms,
            "emitted_chars": event.emitted_chars,
            "cost_estimate": None,
        }

    def close(self, timeout: float = 5.0) -> None:
        try:
            self._queue.put(None, timeout=1.0)
        except queue.Full:
            return
        self._thread.join(timeout)
