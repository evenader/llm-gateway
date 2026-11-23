"""模型路由：逻辑模型名 → ModelSpec。"""
from __future__ import annotations

from dataclasses import dataclass

from gateway.config import ModelConfig, Settings
from gateway.core.errors import GatewayError


@dataclass(frozen=True)
class RouteDecision:
    spec: ModelConfig
    reason: str = "static"


class Router:
    def __init__(self, settings: Settings) -> None:
        self._models = settings.models

    def names(self) -> list[str]:
        return sorted(self._models)

    def resolve(self, model: str) -> RouteDecision:
        spec = self._models.get(model)
        if spec is None:
            raise GatewayError(
                "model_not_found",
                f"未知模型: {model}",
                details={"available_models": self.names()},
            )
        return RouteDecision(spec=spec, reason="static")
