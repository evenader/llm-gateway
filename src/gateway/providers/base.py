"""适配器基类：统一能力、非流式、流式接口；httpx 客户端与生命周期由 provider 自己维护。"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from gateway.config import ModelConfig, Settings
from gateway.core.errors import GatewayError
from gateway.core.types import StreamEvent, UnifiedRequest, UnifiedResponse
from gateway.providers.http_errors import map_httpx_error, map_status
from gateway.providers.sse_parser import SSEEvent, SSEParser


class UpstreamStream:
    """封装上游 httpx 响应与已映射的事件流；关闭时释放连接（流式使用独立 client）。"""

    def __init__(
        self,
        response: httpx.Response,
        events: AsyncIterator[StreamEvent],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._response = response
        self._events = events
        self._client = client
        self._closed = False

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._events

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        aclose = getattr(self._events, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass
        try:
            await self._response.aclose()
        except Exception:
            pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass


async def iter_sse(response: httpx.Response) -> AsyncIterator[SSEEvent]:
    parser = SSEParser()
    try:
        async for chunk in response.aiter_bytes():
            for event in parser.feed(chunk):
                yield event
        for event in parser.close():
            yield event
    except httpx.HTTPError as exc:
        raise map_httpx_error(exc) from exc


class Provider(ABC):
    protocol: str = ""

    def __init__(self, spec: ModelConfig, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.spec = spec
        self.settings = settings
        self._client = client

    @property
    def capabilities(self):
        return self.spec.capabilities

    @property
    def timeout(self):
        return self.settings.timeout_for(self.spec)

    def api_key(self) -> str:
        env = self.spec.api_key_env
        if not env:
            raise GatewayError("internal_error", "模型未配置 api_key_env")
        value = os.environ.get(env)
        if not value:
            raise GatewayError("internal_error", f"环境变量 {env} 未设置")
        return value

    def _make_client(self) -> httpx.AsyncClient:
        timeout = self.timeout
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=timeout.connect_s,
                read=timeout.read_s,
                write=timeout.read_s,
                pool=timeout.connect_s,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    def http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def new_stream_client(self) -> httpx.AsyncClient:
        """流式请求使用独立 client：单条流的取消/关闭不波及共享连接池。"""
        return self._make_client()

    def url(self) -> str:
        return f"{self.spec.base_url.rstrip('/')}{self.spec.endpoint_path}"

    async def _post(
        self,
        headers: dict[str, str],
        body: dict,
        *,
        stream: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        http = client or self.http_client()
        try:
            request = http.build_request("POST", self.url(), headers=headers, json=body)
            return await http.send(request, stream=stream)
        except httpx.HTTPError as exc:
            raise map_httpx_error(exc) from exc

    @staticmethod
    async def _ensure_success(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        body = await response.aread()
        await response.aclose()
        raise map_status(response.status_code, body, response.headers)

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    @abstractmethod
    async def complete(self, request: UnifiedRequest) -> UnifiedResponse: ...

    @abstractmethod
    async def open_stream(self, request: UnifiedRequest) -> UpstreamStream: ...
