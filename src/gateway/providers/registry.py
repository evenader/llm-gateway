"""协议 → 适配器类 的注册表。新增协议 = 新增文件 + 注册一行。"""
from __future__ import annotations

from gateway.config import ConfigError, ModelConfig, Settings
from gateway.providers.anthropic_messages import AnthropicMessagesProvider
from gateway.providers.base import Provider
from gateway.providers.openai_responses import OpenAIResponsesProvider

PROTOCOLS: dict[str, type[Provider]] = {
    "openai_responses": OpenAIResponsesProvider,
    "anthropic_messages": AnthropicMessagesProvider,
}


def create_provider(spec: ModelConfig, settings: Settings) -> Provider:
    provider_cls = PROTOCOLS.get(spec.protocol)
    if provider_cls is None:
        raise ConfigError(f"模型 {spec.upstream_model} 使用未知协议: {spec.protocol}")
    return provider_cls(spec, settings)


def validate_protocols(settings: Settings) -> None:
    unknown = sorted({spec.protocol for spec in settings.models.values() if spec.protocol not in PROTOCOLS})
    if unknown:
        raise ConfigError(f"未知协议: {', '.join(unknown)}")
