# 00 架构设计

> 本文件是 LLM Gateway 的架构唯一依据。实现偏离时必须先回写本文件。

## 1. 目标与非目标

目标：对外提供唯一入口 `POST /v1/chat/completions`，对内用适配器模式调用两种协议的上游，并自证六项能力：流式、结构化输出、提示词模板与版本、可观测性、指数退避重试、按模型限流。

非目标（显式不做）：工具调用 / function calling、多模态、会话与 `previous_response_id`、WebSocket、自动降级 fallback、多租户与计费、前端页面。`Router.resolve()` 预留 `RouteDecision.reason` 字段作为将来降级的扩展点，但本期只有 `static`。

## 2. 项目位置与目录

项目根目录即 `course_code/homework/homework01`（提示词文档位于 `doc/`，蓝图位于 `docs/blueprint/`）。

```
homework01/
├─ README.md  pyproject.toml  .env.example
├─ config/  gateway.yaml  gateway.verify.yaml  prompts/*.yaml
├─ docs/  blueprint/  review-log.md
├─ src/gateway/
│  ├─ main.py              组合根（唯一同时 import 业务与 observability 的地方）
│  ├─ config.py            配置模型与加载（env 校验、快失败）
│  ├─ api/                 routes_chat routes_prompts routes_misc sse error_handlers schemas
│  ├─ core/                types errors events retry ratelimit clock
│  ├─ services/            chat_service router structured prompts
│  ├─ providers/           base registry openai_responses anthropic_messages sse_parser http_errors
│  └─ observability/       plugin recorder store routes stats
├─ tests/                  unit/  mock_upstream/
└─ scripts/                verify_all.py  run_mock_upstream.py  demo_curl.sh
```

## 3. 分层与依赖方向

```
api  ──►  services  ──►  providers
 │           │              │
 └───────────┴──►  core  ◄──┘
main.py ──► 全部业务 + observability（仅此处装配插件）
observability ──► core（订阅事件）；业务层不得 import observability
```

硬规则：

1. 依赖只能由外向内：`api → services → providers / core`，`core` 不 import 任何上层。
2. 供应商原始对象、事件名、SDK 异常不得上浮到 `services` / `api`；适配器只产出 `UnifiedResponse` / `StreamEvent` / `Usage` / `GatewayError`。
3. `api`、`core`、`services`、`providers` 目录下任何模块不得 import `gateway.observability`（M8 用 AST 静态检查）。
4. 模型名、base_url、API Key 只出现在配置或 `providers` 内部；`api/services` 只见逻辑模型名与 `ModelSpec`。

## 4. 模块职责

| 模块 | 职责 |
| --- | --- |
| `config.py` | 解析 yaml → `Settings`；校验模型表/协议/重试/限流；启动时校验 `api_key_env` 存在，缺失则报出变量名并退出 |
| `core/types.py` | `Message`、`ResponseFormat`、`UnifiedRequest`、`UnifiedResponse`、`Usage`、`StreamEvent`（Start/TextDelta/UsageEvent/Done） |
| `core/errors.py` | 唯一错误类型 `GatewayError(code, message, http_status, stage, retryable, details)` 与错误信封构造 |
| `core/events.py` | `EventBus`（同步 emit，订阅者异常隔离）+ 事件：`RequestStarted`、`AttemptFinished`、`FirstTokenReceived`、`RequestFinished` |
| `core/retry.py` | `RetryPolicy` + `Retrier`，sleep/rng/clock 全部注入；只重试 `retryable=true` 的 `GatewayError` |
| `core/ratelimit.py` | 每逻辑模型一个令牌桶，`try_acquire` 同步非阻塞，惰性补充 |
| `core/clock.py` | `Clock` 协议（`monotonic`/`now`）、`SystemClock`、验证用 `OffsetClock` |
| `services/router.py` | `ModelRegistry` + `Router.resolve(model) → RouteDecision(spec, reason)` |
| `services/prompts.py` | 模板存储（独立 SQLite）、种子导入、SemVer 版本解析、单次扫描渲染、静态校验 |
| `services/structured.py` | 结构化请求的 Schema 自校验、本地产出校验（语法/字段/业务钩子）、有限修复循环 |
| `services/chat_service.py` | 用例编排：入口校验 → 路由 → 准入 → 模板 → 能力检查 → 上游调用/重试 → 响应归集 → 事件与 trace |
| `providers/base.py` | `Provider` 抽象：`capabilities`、`complete()`、`open_stream()`、`aclose()` |
| `providers/registry.py` | protocol 字符串 → 适配器类；新增协议只加文件 + 注册一行 |
| `providers/openai_responses.py` | OpenAI Responses 协议翻译与归一 |
| `providers/anthropic_messages.py` | Anthropic Messages 协议翻译与归一 |
| `providers/sse_parser.py` | 增量字节 → SSE 事件，支持多字节切分、CRLF、多行 data、注释行 |
| `providers/http_errors.py` | 唯一把 httpx 异常/上游状态码/上游错误体映射为 `GatewayError` 的地方 |
| `api/schemas.py` | 对外 pydantic v2 模型，`extra=forbid`，与内部类型完全分离 |
| `api/sse.py` | 内部 `StreamEvent` → 对外 SSE 字节的唯一编码处 |
| `api/error_handlers.py` | `GatewayError` / 未预期异常 → 统一信封 |
| `observability/` | 订阅事件、有界队列、后台线程批量写 SQLite、查询路由、统计 |
| `main.py` | `create_app(settings, clock=..., sleep=..., rng=...)` 组合根；装配 provider、service、路由、插件 |

## 5. 数据流

### 5.1 非流式

```
客户端 → middleware(request_id/auth) → api 读取 body
→ ChatService.handle:
   1) pydantic 校验（extra=forbid，字段路径）
   2) Router.resolve（未知模型 404，零上游）
   3) RateLimiter.try_acquire（失败 429，零上游）
   4) PromptService 渲染（模板错误在请求上游前报出）
   5) 能力检查（stream / structured）
   6) StructuredOutputRunner 或单次调用：
      Retrier.run(provider.complete(UnifiedRequest))
   7) 四层成功判定：网络 → HTTP → 协议 → 业务（非空、未截断、校验通过）
→ api 重建对外响应（usage、gateway 元数据）→ 统一信封
```

### 5.2 流式

```
ChatService.prepare_stream:
   1-5 同上
   6) Retrier.run(provider.open_stream + 读取首个有意义事件)
      - 首个有意义事件 = 首个非空 TextDelta / Done / 抛错
      - 此段失败：关闭上游流，返回普通 JSON 错误（正确状态码），按 F8 重试
   7) 提交点：返回 StreamSession；此后不再重试、不换模型
→ api/sse.py 输出 SSE：
     首块 delta.role=assistant → 若干 delta.content → 末块 finish_reason+usage+gateway → [DONE]
     上游沉默超过 heartbeat → `: keep-alive`
     提交后失败 → `event: error`（统一信封）→ 关闭，不再发 [DONE]
     客户端断开 → 取消穿透到上游（关闭 httpx response/连接），trace=cancelled
```

`StreamSession` 内部用后台任务把适配器事件泵入 `asyncio.Queue`，SSE 编码器以 `wait_for` 取事件实现心跳；取消时停泵并 `aclose` 上游。

- 取消检测：`GatewayMiddleware` 声明 ASGI `spec_version=2.4` 之外的默认 2.0 路径由 `StreamingResponse` 监听 `http.disconnect`；SSE 生成器 `finally` 调用 `StreamSession.abort()`，以独立清理任务先关闭上游响应/专用 client，再取消泵任务，确保 TCP 连接被关闭。
- 每条流式请求使用独立 `httpx.AsyncClient`（非流式仍复用共享连接池）：这样单条流的取消不会波及同一模型的其他流，也能保证取消时连接确实被关闭。

### 5.3 结构化输出

```
response_format 自校验（jsonschema.check_schema，非法 422）
→ pro：适配器原生 text.format；flash：service 追加 system 指令（含 Schema 与「JSON」）
→ 本地三层校验：json.loads 严格解析 → jsonschema（收集全部路径/类型）→ 业务钩子
→ 失败且 finish_reason != length 且 repair 预算未用尽：
     原消息 + assistant(上次非法输出) + user(仅错误位置与类型的修正指令，要求只输出 JSON)
→ 修复调用独立套用同一 RetryPolicy；repair_count 与 retry_count 分开计数
→ 成功：content=校验通过的原文，parsed=对象；预算耗尽 502 structured_output_invalid；截断 502 structured_output_truncated
```

### 5.4 错误与 trace

- `ChatService` 是所有 chat 请求 trace 的唯一发射点：入口即 `RequestStarted`，所有出口（成功/失败/拒绝/取消）恰好一次 `RequestFinished`。
- 请求级 422 也走该路径，因此也有一条 trace。
- 流式取消由 SSE 生成器的 `finally` 收尾并 finalize。

## 6. 关键抽象

```python
class Provider(Protocol):
    capabilities: Capabilities
    async def complete(self, req: UnifiedRequest) -> UnifiedResponse: ...
    async def open_stream(self, req: UnifiedRequest) -> UpstreamStream: ...
    async def aclose(self) -> None: ...

@dataclass(frozen=True)
class RouteDecision:
    spec: ModelSpec
    reason: str  # 本期恒为 "static"

class EventBus:
    def subscribe(self, handler: Callable[[object], None]) -> None
    def emit(self, event: object) -> None  # 逐个 handler try/except
```

`UnifiedRequest` 字段：`messages`、`temperature`、`top_p`、`max_tokens`、`stop`、`response_format`、`stream`、`upstream_model`、`thinking`。
`Usage`：`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_creation_tokens`、`reasoning_tokens`、`total_tokens`、`usage_missing`，所有计数允许 `None`，`add()` 逐项累加且不估算。

## 7. 配置驱动

- `config/gateway.yaml`：生产配置（模型表、限流、超时、重试、模板种子目录、可观测开关）。
- `config/gateway.verify.yaml`：验证配置（指向本地 mock；退避 0.5/2/8 且 `jitter=none`、心跳 0.3s、默认限流放宽，并含验证专用逻辑模型）。
- 验证脚本把 yaml 文本中的 `__MOCK_BASE__`、`__TMP__` 替换为实际值后写入临时文件再加载，因此不改代码即可换地址。
- 启动时读取模型 `api_key_env` 指向的环境变量；缺失即失败并列出变量名。

## 8. 扩展点

1. 新协议：新增 `providers/xxx.py` + `providers/registry.py` 注册一行，`api/services` 零改动。
2. 新模型：只改 yaml。
3. 降级：`Router.resolve` 已返回 `RouteDecision`，以后由 Router 内部决策，调用方不变。
4. 可观测性后端替换：替换 `observability/store.py` 适配器即可，业务层不感知。

## 9. 部署与局限

- 单进程 uvicorn；限流与 trace 队列都在进程内，多 worker 部署时限流按进程独立（写入 README）。
- SQLite 用于 trace 与模板，两库分离；trace 写入在后台线程批量提交。
- 日志只记 request_id、异常类型、已发出事件数、用量；不记提示词、输出正文、Key、鉴权头。
