"""组合根：唯一装配所有业务组件的地方（可观测性插件在 M7 接入）。"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.api.error_handlers import install_error_handlers
from gateway.api.routes_chat import router as chat_router
from gateway.api.routes_misc import router as misc_router
from gateway.api.routes_prompts import router as prompts_router
from gateway.config import Settings, load_settings, retry_policy_from
from gateway.core.clock import Clock, SystemClock
from gateway.core.errors import GatewayError
from gateway.core.events import EventBus
from gateway.core.ratelimit import RateLimiter
from gateway.core.retry import Retrier
from gateway.providers.registry import create_provider, validate_protocols
from gateway.services.chat_service import ChatService
from gateway.services.prompts import PromptService
from gateway.services.router import Router


def _check_auth(headers: Headers, settings: Settings) -> GatewayError | None:
    header = headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return GatewayError("auth_failed", "缺少网关鉴权凭证")
    token = header[7:].strip()
    allowed = {
        item.strip()
        for item in os.environ.get(settings.auth.keys_env, "").split(",")
        if item.strip()
    }
    if not token or token not in allowed:
        return GatewayError("auth_failed", "网关鉴权失败")
    return None


class GatewayMiddleware:
    """纯 ASGI 中间件：注入 request_id、可选网关鉴权、统一响应头。"""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        if self.settings.auth.enabled:
            auth_error = _check_auth(Headers(scope=scope), self.settings)
            if auth_error is not None:
                response = JSONResponse(
                    auth_error.envelope(request_id),
                    status_code=auth_error.http_status,
                    headers={"X-Request-ID": request_id},
                )
                await response(scope, receive, send)
                return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        await self.app(scope, receive, send_with_header)


def create_app(
    settings: Settings | str | Path,
    *,
    clock: Clock | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    rng=None,
) -> FastAPI:
    if not isinstance(settings, Settings):
        settings = load_settings(settings)
    validate_protocols(settings)

    resolved_clock: Clock = clock or SystemClock()
    resolved_sleep = sleep or asyncio.sleep
    providers = {name: create_provider(spec, settings) for name, spec in settings.models.items()}
    bus = EventBus()
    retrier = Retrier(
        retry_policy_from(settings), sleep=resolved_sleep, clock=resolved_clock, rng=rng
    )
    limiter = RateLimiter(
        default_rate_per_s=settings.defaults.rate_limit.rate_per_s,
        default_burst=settings.defaults.rate_limit.burst,
        clock=resolved_clock,
    )
    router = Router(settings)
    prompt_service = PromptService(settings.prompts)
    prompt_service.seed_from_dir()
    service = ChatService(
        settings=settings,
        router=router,
        providers=providers,
        limiter=limiter,
        retrier=retrier,
        bus=bus,
        clock=resolved_clock,
        prompt_service=prompt_service,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            for provider in providers.values():
                await provider.aclose()
            if observability_handle is not None:
                observability_handle.close()

    app = FastAPI(title="LLM Gateway", lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = resolved_clock
    app.state.sleep = resolved_sleep
    app.state.event_bus = bus
    app.state.router = router
    app.state.chat_service = service
    app.state.provider_map = providers
    app.state.prompt_service = prompt_service

    install_error_handlers(app)
    app.add_middleware(GatewayMiddleware, settings=settings)

    app.include_router(chat_router)
    app.include_router(misc_router)
    app.include_router(prompts_router)

    observability_handle: Any | None = None
    if settings.observability.enabled:
        # 只有组合根同时引用业务与 observability
        from gateway.observability.plugin import install as install_observability

        observability_handle = install_observability(app, bus, settings)
    app.state.observability = observability_handle
    return app
