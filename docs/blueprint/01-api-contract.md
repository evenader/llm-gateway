# 01 对外 API 契约

状态码约定：成功 200/201；请求不合法 422 或 400；未知模型/模板 404；重复创建 409；限流 429；上游问题按 03 映射。所有错误响应体使用统一信封。

## 1. POST /v1/chat/completions

### 1.1 请求（`extra="forbid"`，未知字段 → 422 `invalid_request`，`details.fields` 给出字段路径）

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `model` | string | 是 | 配置中的逻辑模型名 |
| `messages` | array | 二选一 | 元素 `{role: system\|user\|assistant, content: string}` |
| `prompt` | object | 二选一 | `{name, version?, variables}`；`version` 缺省或 `latest` = 最高 SemVer |
| `stream` | bool | 否 | 默认 false |
| `temperature` | float | 否 | [0, 2] |
| `top_p` | float | 否 | (0, 1] |
| `max_tokens` | int | 否 | ≥ 1 |
| `stop` | array[string] | 否 | ≤ 16 项 |
| `response_format` | object | 否 | `{type: text\|json_object\|json_schema, json_schema?: {name, schema, strict}}` |

组合规则：

- `messages` 与 `prompt` 至少给一个，`messages: []` 视为空 → 422 `invalid_request`。
- 同时给出时：模板渲染消息在前、`messages` 追加在后；此时 `messages` 不得含 `system` → 422 `invalid_request`。
- `stream=true` 与 `response_format.type != text` 同时给 → 400 `unsupported_parameter_combination`，上游零调用。
- `json_schema` 仅在 `type=json_schema` 时允许且必填；请求内 Schema 本身非法 → 422 `invalid_response_format`。

### 1.2 非流式响应

```json
{
  "id": "<request_id>",
  "object": "chat.completion",
  "created": 1730000000,
  "model": "deepseek-v4-pro",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "...", "parsed": {"...": "仅结构化模式出现"}},
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "total_tokens": 150,
    "prompt_tokens_details": {"cached_tokens": 30},
    "completion_tokens_details": {"reasoning_tokens": 20}
  },
  "gateway": {
    "request_id": "<request_id>",
    "upstream_model": "deepseek-v4-pro",
    "protocol": "openai_responses",
    "retry_count": 0,
    "repair_count": 0,
    "latency_ms": 812.4,
    "prompt": {"name": "code-review", "version": "1.1.0"}
  }
}
```

- `usage` 任一计数上游缺失时为 `null`，不估算。
- `gateway.prompt` 仅在引用模板时出现，否则为 `null`。
- 响应头 `X-Request-ID` 与 `id` 一致。

### 1.3 流式响应（SSE）

响应头：`Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`、`X-Request-ID`。

```
data: {"id":"...","object":"chat.completion.chunk","created":...,"model":"...","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {...,"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {...,"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{...},"gateway":{...}}

data: [DONE]
```

- 首块必为 `delta.role=assistant`；末块带 `finish_reason`、`usage`、`gateway`；最后 `data: [DONE]`。
- 心跳：上游沉默超过 `stream.heartbeat_interval_s` 时发送 `: keep-alive` 注释行。
- 提交点（首块已发出）之后失败：发送

```
event: error
data: {"error": {统一错误信封}}

```

然后关闭连接，不再发送 `[DONE]`。提交点之前失败：普通 JSON 错误响应 + 正确 HTTP 状态码（其中可重试错误按 F8 重试）。

## 2. 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/models` | 逻辑模型名、protocol、capabilities、默认 max_tokens；不暴露 base_url / env 名 |
| GET | `/healthz` | `{"status":"ok"}` |
| GET | `/readyz` | 存储可用性检查，`{"status":"ok"}` |
| POST | `/v1/prompts` | 创建新版本，201；同 name+version → 409 `template_conflict` |
| GET | `/v1/prompts` | 模板列表（name + 版本集合） |
| GET | `/v1/prompts/{name}/versions/{version\|latest}` | 单版本详情 |
| POST | `/v1/prompts/{name}/versions/{version\|latest}/render` | 仅渲染预览，不调用上游；body `{variables}`，返回 `{name, version, messages}` |
| PUT / DELETE | `/v1/prompts/...` | 405（版本不可变，不提供更新/删除） |
| GET | `/admin/observability/traces` | 过滤 `model`、`status`、`since`、`limit` |
| GET | `/admin/observability/traces/{request_id}` | 单条 trace |
| GET | `/admin/observability/stats` | 按模型聚合统计 |

模板创建请求：

```json
{
  "name": "code-review",
  "version": "1.2.0",
  "description": "代码审查",
  "owner": "platform",
  "changelog": "新增可观测性维度",
  "messages": [
    {"role": "system", "content": "你是代码审查工程师……"},
    {"role": "user", "content": "请审查：\n{{ code }}"}
  ],
  "variables": [
    {"name": "code", "required": true, "max_length": 20000},
    {"name": "language", "required": false, "default": "python"}
  ]
}
```

创建时静态检查（失败 422 `template_render_error`，`details.fields` 给出路径）：

- `version` 必须是 `X.Y.Z`；
- 占位符 `{{ name }}` 必须全部声明；
- 声明变量必须全部被使用；
- `role` 只能是 system/user/assistant。

调用时：缺必填变量 → 422 `template_variable_missing`；多传未声明变量、变量类型非 `str/int/float/bool`、值超 `max_length`（默认 20000）→ 422 `template_render_error`；模板或版本不存在 → 404 `template_not_found`。

## 3. 网关自身鉴权

配置 `auth.enabled=true` 时所有端点要求 `Authorization: Bearer <key>`，key 集合来自 `auth.keys_env` 指向的环境变量（逗号分隔）。失败 401 `auth_failed`。默认关闭。

## 4. 错误信封

```json
{
  "error": {
    "code": "rate_limit_exceeded",
    "message": "模型 deepseek-v4-pro 超出限流",
    "type": "rate_limit_error",
    "stage": "admission",
    "retryable": false,
    "request_id": "8f...",
    "details": {"model": "deepseek-v4-pro", "rate_per_s": 5, "burst": 10, "retry_after_s": 1}
  }
}
```

字段：`code`、`message`、`type`、`stage`、`retryable`、`request_id`、`details`（可选）。`message` 不含 Key、鉴权头、堆栈、上游响应全文；上游摘要脱敏后 ≤ 300 字符。限流响应带 `Retry-After: <整数秒 ≥ 1>`。

## 5. 示例（完整 curl 清单见 README / scripts/demo_curl.sh）

```bash
curl -s http://127.0.0.1:8000/healthz
curl -s -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}]}'
curl -N -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```
