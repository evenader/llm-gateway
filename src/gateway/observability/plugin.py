"""可观测性插件装配：订阅事件、挂载查询路由、关机 flush。"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI

from gateway.config import Settings
from gateway.core.events import EventBus
from gateway.observability.recorder import Recorder
from gateway.observability.routes import router
from gateway.observability.store import TraceStore


@dataclass
class ObservabilityHandle:
    recorder: Recorder
    store: TraceStore

    def close(self) -> None:
        self.recorder.close()


def install(app: FastAPI, bus: EventBus, settings: Settings) -> ObservabilityHandle:
    store = TraceStore(settings.observability.db_path)
    recorder = Recorder(store, queue_size=settings.observability.queue_size)
    bus.subscribe(recorder)
    app.include_router(router)
    handle = ObservabilityHandle(recorder=recorder, store=store)
    app.state.observability = handle
    return handle
