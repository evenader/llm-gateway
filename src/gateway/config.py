"""配置模型与加载。模型表、协议、限流、超时、重试全部配置驱动。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gateway.core.retry import RetryPolicy


class ConfigError(RuntimeError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Strict):
    host: str = "0.0.0.0"
    port: int = 8000


class AuthConfig(_Strict):
    enabled: bool = False
    keys_env: str = "GATEWAY_API_KEYS"


class RetryConfig(_Strict):
    max_retries: int = 3
    base_delay_s: float = 0.5
    factor: float = 2.0
    max_delay_s: float = 8.0
    jitter: Literal["none", "full", "equal"] = "full"


class StreamConfig(_Strict):
    heartbeat_interval_s: float = 15.0


class StructuredConfig(_Strict):
    max_repairs: int = 2


class TimeoutConfig(_Strict):
    connect_s: float = 5.0
    read_s: float = 60.0
    total_s: float = 120.0


class RateLimitConfig(_Strict):
    rate_per_s: float = 5.0
    burst: int = 10


class ObservabilityConfig(_Strict):
    enabled: bool = True
    db_path: str = "data/observability.db"
    queue_size: int = 10000


class PromptsConfig(_Strict):
    db_path: str = "data/prompts.db"
    seed_dir: str = "config/prompts"


class CapabilitiesConfig(_Strict):
    stream: bool = True
    native_json_object: bool = False
    native_json_schema: bool = False
    structured_output: bool = True


class ModelConfig(_Strict):
    protocol: str
    base_url: str
    endpoint_path: str
    api_key_env: str | None = None
    upstream_model: str
    timeout: TimeoutConfig | None = None
    rate_limit: RateLimitConfig | None = None
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)
    thinking: Literal["enabled", "disabled"] = "disabled"
    default_max_tokens: int | None = None
    default_temperature: float | None = None
    default_top_p: float | None = None

    @field_validator("endpoint_path")
    @classmethod
    def _endpoint_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("endpoint_path 必须以 / 开头")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url_scheme(cls, value: str) -> str:
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("base_url 必须是 http(s) 地址")
        return value


class DefaultsConfig(_Strict):
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)


class Settings(_Strict):
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    structured: StructuredConfig = Field(default_factory=StructuredConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    models: dict[str, ModelConfig]

    @model_validator(mode="after")
    def _models_not_empty(self) -> "Settings":
        if not self.models:
            raise ValueError("models 不能为空")
        return self

    def timeout_for(self, spec: ModelConfig) -> TimeoutConfig:
        return spec.timeout or self.defaults.timeout

    def rate_limit_for(self, spec: ModelConfig) -> RateLimitConfig:
        return spec.rate_limit or self.defaults.rate_limit

    def validate_env(self) -> None:
        missing: list[str] = []
        for spec in self.models.values():
            if spec.api_key_env and spec.api_key_env not in os.environ:
                missing.append(spec.api_key_env)
        if self.auth.enabled and self.auth.keys_env not in os.environ:
            missing.append(self.auth.keys_env)
        if missing:
            unique = sorted(set(missing))
            raise ConfigError(f"缺少环境变量: {', '.join(unique)}")


def load_settings(path: str | Path, *, require_env: bool = True) -> Settings:
    raw = Path(path).read_text(encoding="utf-8")
    data: Any = yaml.safe_load(raw) or {}
    try:
        settings = Settings.model_validate(data)
    except Exception as exc:  # pydantic 错误转成配置错误，启动快失败
        raise ConfigError(f"配置校验失败: {exc}") from exc
    if require_env:
        settings.validate_env()
    return settings


def retry_policy_from(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        max_retries=settings.retry.max_retries,
        base_delay_s=settings.retry.base_delay_s,
        factor=settings.retry.factor,
        max_delay_s=settings.retry.max_delay_s,
        jitter=settings.retry.jitter,
    )
