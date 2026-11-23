"""时钟抽象：重试预算与限流使用可注入时钟；延迟测量使用真实单调时钟。"""
from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now(self) -> float: ...


class SystemClock:
    """生产时钟。"""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> float:
        return time.time()


class OffsetClock(SystemClock):
    """验证用时钟：真实时间 + 可手动推进的偏移，用于限流恢复与预算耗尽用例。"""

    def __init__(self) -> None:
        self._offset = 0.0

    def advance(self, seconds: float) -> None:
        self._offset += seconds

    def monotonic(self) -> float:
        return time.monotonic() + self._offset

    def now(self) -> float:
        return time.time() + self._offset
