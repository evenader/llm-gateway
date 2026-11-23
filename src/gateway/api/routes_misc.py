"""模型列表与健康检查。"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request):
    settings = request.app.state.settings
    data = []
    for name in sorted(settings.models):
        spec = settings.models[name]
        data.append(
            {
                "id": name,
                "object": "model",
                "protocol": spec.protocol,
                "capabilities": spec.capabilities.model_dump(),
                "default_max_tokens": spec.default_max_tokens,
            }
        )
    return {"object": "list", "data": data}


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    return {"status": "ok"}
