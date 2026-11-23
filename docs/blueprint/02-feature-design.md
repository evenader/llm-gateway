# 02 功能设计

每个功能给出设计要点与对应验证编号。验证编号的完整集合与判定见 `04-verification-plan.md`。

## F1 统一抽象层与路由

- `ModelRegistry` 从 `gateway.yaml` 载入 `ModelSpec`；启动时校验协议已注册、`endpoint_path` 非空、`api_key_env` 存在（缺失 → 启动失败并指出变量名）。
- `Router.resolve(model)` 返回 `RouteDecision{spec, reason="static"}`；未知模型 404 `model_not_found`，`details.available_models` 列出可用逻辑模型。
- `ProviderRegistry`：`protocol → provider 类`。每个模型实例化自己的 provider（各自持有 `httpx.AsyncClient`，复用连接池，关机统一 `aclose`）。
- 能力检查在 service 层：请求 `stream` 而 `capabilities.stream=false` → 400 `unsupported_capability`，零上游；`structured_output=false` 同理。`native_json_object/schema` 只决定结构化路径（原生或提示词注入），不构成拒绝条件。
- 非透明代理：入口 `extra=forbid`；适配器从 `UnifiedRequest` 重建上游请求；客户端请求头一律不转发（鉴权由适配器读环境变量自行生成）。
- `finish_reason` 统一为 `stop / length / content_filter / error`。

验证：V-ROUTE-01 ~ V-ROUTE-07。

## F2 协议适配器与 Usage 归一

### OpenAI Responses（deepseek-v4-pro）

- 请求：`POST {base_url}{endpoint_path}`，`Authorization: Bearer <key>`。
- Body：`model=upstream_model`；`instructions`=所有 system 消息按 `\n` 合并；`input`=其余消息 `{role, content}`；`max_output_tokens`；`temperature`；`top_p`；`stream`；结构化时 `text.format`（`json_object` 或 `json_schema{name,schema,strict}`）；`thinking=disabled` 时 `reasoning.effort="none"`。不发送 `store/previous_response_id/stream_options/metadata/messages/system/max_tokens`。
- 响应：遍历 `output` 中 `type=message` 项的 `content`，拼接 `type=output_text` 的 `text`。`status=completed→stop`；`incomplete` 且 `incomplete_details.reason=max_output_tokens→length`，其它 incomplete 原因→`content_filter`；`failed` 或带 `error`→ 按 F7 映射。`status` 缺失视为协议错误。
- 流式：`response.output_text.delta`（`delta` 非空）→ `TextDelta`；忽略 `response.reasoning_text.delta` 等；终止事件 `response.completed / response.incomplete / response.failed`，其 `response.usage` 为最终用量；无 `[DONE]`。
- Usage：`input=usage.input_tokens`；`cache_read=input_tokens_details.cached_tokens`；`output=output_tokens`；`reasoning=output_tokens_details.reasoning_tokens`；`total=total_tokens` 缺失则 input+output。

### Anthropic Messages（deepseek-v4-flash）

- 请求：`POST {base_url}{endpoint_path}`，`x-api-key`、`anthropic-version: 2023-06-01`。
- Body：`model`；`max_tokens`（必带，缺省 `default_max_tokens`）；`system` 为顶层合并字符串；`messages` 仅 user/assistant，相邻同角色合并（`\n\n` 拼接），首条非 user 时前置空 user；`temperature`、`top_p`、`stop_sequences`、`stream`；`thinking=disabled` 时 `thinking.type="disabled"`；不发送 `response_format`。
- 响应：`content` 中 `type=text` 拼接；`stop_reason`：`end_turn/stop_sequence→stop`、`max_tokens→length`、`refusal→content_filter`。
- 流式：`message_start`（初始 usage）→ `content_block_start` → `content_block_delta`（仅 `text_delta` 产出 TextDelta，`thinking_delta` 忽略）→ `content_block_stop` → `message_delta`（`stop_reason` 与最终 usage）→ `message_stop`；`ping` 忽略；`event: error` 或无事件名的 error data 帧 → 错误；EOF 前无 `message_stop` → `stream_interrupted`。
- Usage：`input = input_tokens + cache_read_input_tokens + cache_creation_input_tokens`；`cache_read / cache_creation` 取对应字段；`output=output_tokens`；`reasoning=null`；`total=input+output`。流式以 `message_delta.usage` 为准，缺失字段才用 `message_start` 补，禁止相加。

### 通用

- 上游无 usage → 全字段 `null` 且 `usage_missing=true`；不估算。结构化多次调用 usage 逐项累加（`Usage.add`）。
- 鉴权差异只在适配器内部；Key 只在 provider 层读取环境变量。
- 映射 httpx 异常、状态码、上游错误体统一在 `providers/http_errors.py`，表驱动，见 03。

验证：V-ADP-01 ~ V-ADP-08。

## F3 流式输出

- `sse_parser`：增量 bytes → 增量 UTF-8 解码 → 归一 CRLF/LF → 按空行切事件；支持 `event`、多行 `data`、`id`、`retry`、`:` 注释；不依赖 chunk 边界。
- 适配器只产出内部 `StreamEvent`，`api/sse.py` 统一编码对外 SSE，供应商事件名不下发。
- 提交点：`Retrier.run` 内完成「打开上游流 + 读首个有意义事件」；失败则返回普通 JSON 错误并按 F8 重试。提交后失败只发 `event: error`，不重试、不换模型。
- 终态：`completed / failed / cancelled`，每个流恰好一个；由 `StreamSession` 的 `finalize` 幂等保证。
- 取消：SSE 生成器 `finally` 取消泵任务 → `await` 泵结束 → 关闭上游 httpx response → trace `cancelled`；用 `try/finally` 释放。
- 心跳：SSE 生成器对队列 `wait_for(heartbeat)`，超时输出 `: keep-alive`。
- TTFT：从网关收到请求到首个非空文本 delta；思考类 delta 不算；非流式为 `null`。`generation_ms = total_ms - ttft_ms`（流式），非流式为 `null`。
- 流式 usage 只取上游终止事件。

验证：V-STREAM-01 ~ V-STREAM-14。

## F4 结构化输出

- `response_format.type=json_schema` 时先 `jsonschema.Draft202012Validator.check_schema`，非法 → 422 `invalid_response_format`（零上游）。
- 两条路径、同一校验：pro 用原生 `text.format`；flash 在 system 追加「只输出符合以下 JSON Schema 的合法 JSON，不要代码围栏、不要附加说明」并附紧凑 Schema；`json_object` 模式指令必须含「JSON」。
- 本地三层校验：① `json.loads` 严格解析（不去代码围栏、不自动修复）；② jsonschema 收集全部 `path+type`；③ 业务钩子 `validators`（默认空）。`json_object` 要求顶层为对象。
- 修复循环：校验失败且 `finish_reason != length` 且 `repair_count < max_repairs` 时，追加 `assistant(上次非法输出)` + `user(修正指令)`，指令只含错误位置与类型，要求「只修正出错语法和字段，其余保持不变，只输出 JSON」。修复调用独立套用同一重试策略，预算分开计数。
- `finish_reason=length` 且校验失败 → 502 `structured_output_truncated`，不修复；预算耗尽 → 502 `structured_output_invalid`，`details` 仅含最后一次错误与 `repair_count`，不把非法输出当成功返回。
- 成功响应 `content` 为通过校验的原文，`parsed` 为对象，`gateway.repair_count` 记录次数，usage 为所有调用累加。

验证：V-STRUCT-01 ~ V-STRUCT-11。

## F5 提示词模板与版本

- 数据模型：`name`、`version`（SemVer，创建者指定、同 name 唯一不可变）、`description`、`owner`、`changelog`、`messages`、`variables{name, required, default, max_length}`、`created_at`。存于独立 `prompts.db`。
- 引用与解析：`prompt.name + prompt.version`；`version` 缺省或 `latest` → 最高 SemVer。响应与 trace 记录实际版本。
- 渲染：自写单次扫描 `\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}` 替换，不递归；不使用 `eval/str.format/Jinja`。变量值仅 `str/int/float/bool` 转字符串；超 `max_length`（默认 20000）拒绝。
- 校验：创建时静态检查占位符/声明/角色/版本；调用时缺必填 → `template_variable_missing`，多传/类型非法/超长 → `template_render_error`。全部在上游调用前报出。
- 指令与数据分离：角色规则只能写在模板里；额外 `messages` 只能 user/assistant 且不得含 system；变量原样出现在模板指定位置，不二次渲染。
- 种子：`config/prompts/*.yaml` 启动幂等导入；同 name+version 内容不一致 → 启动报错退出，不覆盖。种子包含 code-review 1.0.0 / 1.1.0、summarize 1.0.0。
- 模板路径不经过可观测性代码；trace 只记 `prompt_name/version/fingerprint/prompt_chars`。

验证：V-TPL-01 ~ V-TPL-14。

## F6 可观测性插件

- `EventBus.emit` 同步调用所有订阅者，逐个 `try/except` 隔离；事件：`RequestStarted`、`AttemptFinished`、`FirstTokenReceived`、`RequestFinished`（唯一终态，携带完整汇总）。
- `plugin.install(app, bus, settings)`：创建 `Recorder` 订阅总线、挂载 `/admin/observability/*`、关机 `flush`；`main.py` 按 `observability.enabled` 决定调用。
- Recorder：`RequestFinished` → 组装行 → `queue.put_nowait`（满则 `dropped += 1`）；后台线程批量写 SQLite（WAL、`CREATE TABLE IF NOT EXISTS`）；写异常计数不抛出。
- 每次 chat 请求恰好一条 trace（completed/failed/rejected/cancelled）。字段：
  `request_id, ts_start, logical_model, upstream_model, protocol, stream, prompt_name, prompt_version, prompt_fingerprint, prompt_chars, output_chars, status, finish_reason, error_code, error_stage, retry_count, repair_count, attempts(JSON: index/duration_ms/error_code/backoff_ms), input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, reasoning_tokens, total_tokens, usage_missing, latency_total_ms, ttft_ms, generation_ms, pre_upstream_ms, retry_wait_ms, emitted_chars, cost_estimate(null)`。
- 延迟拆分：`pre_upstream_ms`（校验/路由/限流/模板）、`retry_wait_ms`（退避总和）、`ttft_ms`、`generation_ms`；总延迟用真实单调时钟测量。
- 查询：traces 过滤 `model/status/since/limit`；单条；stats 按模型聚合次数、成功率、token 总和、total/ttft 的 avg/p50/p95、重试总次数、error_code 计数。
- 隐私：只存指纹与字符数。订阅者异常、队列满、存储不可写都不影响请求。
- 解耦：`api/core/services/providers` 零 import observability；关闭插件后业务照常，`/admin/observability/*` 404。

验证：V-OBS-01 ~ V-OBS-12。

## F7 错误体系与分类

- 唯一错误类型 `GatewayError`；`providers/http_errors.py` 是唯一映射点（表驱动，见 03），解析上游错误体失败时退回纯状态码映射。
- 阶段：request → admission → template → routing → connect → upstream_http → protocol → output_validation → stream → internal。
- 四层成功判定：网络、HTTP、协议（可解析且关键字段齐全）、业务（非空、未截断、校验通过）。HTTP 200 不等于成功。
- 全局 handler 输出统一信封；未预期异常 → 500 `internal_error` 且产生 trace。脱敏 `sanitize()` 去 Key/Bearer/长随机串，摘要 ≤ 300 字符。
- 错误响应头 `X-Request-ID` 与 `error.request_id` 一致。
- 不做自动降级；失败原因写入 trace。

验证：V-ERR-01 ~ V-ERR-07。

## F8 指数退避重试

- `RetryPolicy(max_retries, base_delay_s, factor, max_delay_s, jitter)` + `Retrier`，`sleep/rng/clock` 构造注入；`max_retries=3` → 首次 + 3 次 = 最多 4 次上游调用。
- `d_n = min(max_delay_s, base* factor^(n-1))`；jitter：none=`d_n`、full=`uniform(0,d_n)`、equal=`d_n/2+uniform(0,d_n/2)`。
- 只重试 `retryable=true` 的 `GatewayError`；上游 `Retry-After` → `delay=max(d_n, retry_after)`；`retry_after > max_delay_s` → 不重试立即返回。
- 总预算 `timeout.total_s` 覆盖整个请求（含重试与修复）；剩余时间不足以等待时不再重试。
- 每次尝试产生 `AttemptFinished`（序号、耗时、错误码、退避量），trace `attempts` 与 mock 调用次数一致；网关重试计数以自身为准。
- 作用域：非流式整次调用；流式仅提交点之前；结构化每次修复调用独立套用。限流令牌在入口消耗一次，重试与修复不再消耗。

验证：V-RETRY-01 ~ V-RETRY-11。

## F9 按模型独立限流

- 每个逻辑模型一个令牌桶（`rate_per_s`、`burst`，初始满桶，惰性补充，无后台线程）。
- 准入 `try_acquire(1)` 同步非阻塞；失败 429 `rate_limit_exceeded`，`Retry-After=ceil(缺口/rate)`（≥1），`details{model, rate_per_s, burst, retry_after_s}`。
- 流式与非流式同样受限，在建立上游连接前判定；被拒请求不重试、不调上游、产生 trace `status=rejected`。
- 一次客户端请求只消耗一个令牌；时钟可注入；未配置的模型用 `defaults.rate_limit`；进程内限流，多 worker 局限写入 README。

验证：V-RL-01 ~ V-RL-08。
