"""提示词模板管理端点：创建、列表、版本查询、渲染预览；版本不可变。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request

from gateway.core.errors import GatewayError

router = APIRouter()


async def _read_json(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise GatewayError("invalid_request", "请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise GatewayError("invalid_request", "请求体必须是 JSON 对象")
    return payload


@router.post("/v1/prompts", status_code=201)
async def create_prompt(request: Request):
    payload = await _read_json(request)
    return request.app.state.prompt_service.create(payload)


@router.get("/v1/prompts")
async def list_prompts(request: Request):
    return {"object": "list", "data": request.app.state.prompt_service.list_templates()}


@router.get("/v1/prompts/{name}/versions/{version}")
async def get_prompt(request: Request, name: str, version: str):
    resolved = None if version == "latest" else version
    return request.app.state.prompt_service.get_public(name, resolved)


@router.post("/v1/prompts/{name}/versions/{version}/render")
async def render_prompt(request: Request, name: str, version: str):
    payload = await _read_json(request)
    variables = payload.get("variables") or {}
    resolved = None if version == "latest" else version
    rendered = request.app.state.prompt_service.render(name, resolved, variables)
    return {
        "name": rendered.name,
        "version": rendered.version,
        "messages": rendered.messages,
        "fingerprint": rendered.fingerprint,
        "chars": rendered.chars,
    }


@router.put("/v1/prompts/{rest:path}")
@router.delete("/v1/prompts/{rest:path}")
async def immutable_prompt(rest: str):
    raise GatewayError(
        "invalid_request",
        "模板版本不可变，不提供更新与删除",
        http_status=405,
        details={"path": f"/v1/prompts/{rest}"},
    )
