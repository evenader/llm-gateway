"""可观测性查询端点（由插件挂载；关闭插件时整组路由不存在）。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from gateway.core.errors import GatewayError
from gateway.observability.store import TraceStore

router = APIRouter(prefix="/admin/observability")


def _store(request: Request) -> TraceStore:
    return request.app.state.observability.store


@router.get("/traces")
async def list_traces(
    request: Request,
    model: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 100,
):
    rows = _store(request).query(model=model, status=status, since=since, limit=limit)
    return {"object": "list", "data": rows}


@router.get("/traces/{request_id}")
async def get_trace(request: Request, request_id: str):
    row = _store(request).get(request_id)
    if row is None:
        raise GatewayError("trace_not_found", f"trace {request_id} 不存在", details={"request_id": request_id})
    return row


@router.get("/stats")
async def get_stats(request: Request):
    return _store(request).stats()
