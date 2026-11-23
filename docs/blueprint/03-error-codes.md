# 03 错误码与错误处理

## 1. 统一错误信封

```json
{"error": {"code": "...", "message": "...", "type": "...", "stage": "...", "retryable": false, "request_id": "...", "details": {}}}
```

`details` 可选。`message` 为脱敏摘要；上游原因 ≤ 300 字符。`Retry-After` 仅网关自身限流（429）返回。

## 2. 错误码表

| code | HTTP | stage | 网关是否重试 | type | 触发条件 |
| --- | --- | --- | --- | --- | --- |
| invalid_request | 422 | request | 否 | invalid_request_error | 缺字段、未知字段、取值越界、messages 为空、prompt+messages 冲突 |
| unsupported_parameter_combination | 400 | request | 否 | invalid_request_error | stream 与 response_format≠text 同用 |
| invalid_response_format | 422 | request | 否 | invalid_request_error | 请求中的 JSON Schema 非法 |
| unsupported_capability | 400 | routing | 否 | invalid_request_error | 模型不具备请求的能力（stream/structured） |
| model_not_found | 404 | routing | 否 | invalid_request_error | model 不在配置白名单 |
| auth_failed | 401 | admission | 否 | gateway_error | 网关鉴权开启且失败 |
| rate_limit_exceeded | 429 | admission | 否 | rate_limit_error | 网关按模型限流超限，带 Retry-After |
| template_not_found | 404 | template | 否 | invalid_request_error | 模板或版本不存在 |
| template_variable_missing | 422 | template | 否 | invalid_request_error | 缺必填变量 |
| template_render_error | 422 | template | 否 | invalid_request_error | 未声明变量、类型非法、超长、创建时静态检查失败 |
| template_conflict | 409 | template | 否 | invalid_request_error | 同 name+version 重复创建 |
| upstream_connection_error | 502 | connect | 是 | upstream_error | DNS、连接失败、连接被重置 |
| upstream_timeout | 504 | connect | 是 | upstream_error | 连接或读取超时 |
| upstream_rate_limited | 503 | upstream_http | 是 | upstream_error | 上游 429 |
| upstream_server_error | 502 | upstream_http | 是 | upstream_error | 上游 500 / 502 / 504 |
| upstream_overloaded | 503 | upstream_http | 是 | upstream_error | 上游 503 / 529 |
| upstream_auth_error | 502 | upstream_http | 否 | upstream_error | 上游 401 / 403 |
| upstream_insufficient_balance | 502 | upstream_http | 否 | upstream_error | 上游 402 |
| upstream_bad_request | 502 | upstream_http | 否 | upstream_error | 上游 400 / 404 / 422 |
| upstream_protocol_error | 502 | protocol | 否 | upstream_error | 200 但无法解析或缺关键字段 |
| upstream_empty_response | 502 | protocol | 否 | upstream_error | 200 但没有任何文本 |
| stream_interrupted | SSE error 帧 | stream | 否 | upstream_error | 流中途断开或没有终止事件 |
| structured_output_invalid | 502 | output_validation | 否 | structured_output_error | 修复次数用尽仍不合法 |
| structured_output_truncated | 502 | output_validation | 否 | structured_output_error | 因 max_tokens 截断，修复无意义 |
| internal_error | 500 | internal | 否 | gateway_error | 未预期异常 |
| trace_not_found | 404 | internal | 否 | gateway_error | 查询不存在的 trace（管理端点专用） |

## 3. 映射规则（`providers/http_errors.py`）

- 状态码：429→`upstream_rate_limited(503, retryable)`；500/502/504→`upstream_server_error(502, retryable)`；503/529→`upstream_overloaded(503, retryable)`；401/403→`upstream_auth_error(502)`；402→`upstream_insufficient_balance(502)`；400/404/422→`upstream_bad_request(502)`；其余 4xx→`upstream_bad_request(502)`；其余 5xx→`upstream_server_error(502, retryable)`。
- httpx 异常：`TimeoutException→upstream_timeout(504, retryable)`；`ConnectError/NetworkError/ReadError/WriteError/RemoteProtocolError/ProtocolError→upstream_connection_error(502, retryable)`；其它 `HTTPError→upstream_connection_error(502, retryable)`。
- 上游错误体解析：Anthropic 风格 `{type:"error", error:{type,message}}`、OpenAI 风格 `{error:{message,code}}`；取 message 脱敏截断 300 字符放入 `details.upstream_message`；解析失败退回纯状态码映射。
- `Retry-After` 解析：秒数或 HTTP 日期；放入 `details.retry_after_s`（秒，浮点）。

## 4. 脱敏与日志

- `sanitize(text)`：移除 `Bearer xxx`、`sk-...`、`x-api-key` 值、长度 ≥ 24 的随机串；压缩空白；截断 300 字符。
- 上游 401/403 额外记 error 级日志（凭证问题，含 request_id，不含 Key）。
- 内部日志只记：异常类型、request_id、已发出事件数、用量；禁止记录完整提示词、完整输出、Key、鉴权头、上游堆栈。
- 错误响应不包含上游原始响应全文；`details` 只放结构化字段（`model`、`retry_after_s`、`fields`、`upstream_status`、`upstream_message`（脱敏）、`errors`（结构化错误路径/类型）、`repair_count`、`available_models`）。
- 结构化输出失败时 `details` 不含非法输出正文。

## 5. 处置矩阵

| 阶段 | 示例 | 处置 |
| --- | --- | --- |
| request | 缺字段、越界、组合非法 | 立即 4xx，零上游 |
| admission | 鉴权、限流 | 立即终止，不重试；限流产生 rejected trace |
| template | 变量缺失/非法 | 立即 4xx，零上游 |
| routing | 未知模型、能力不足 | 立即 4xx，零上游 |
| connect / upstream_http | 超时、断连、429、5xx | 有界指数退避重试 |
| protocol | 200 不可解析、空文本 | 不重试（不是临时性问题） |
| output_validation | 截断、非法 JSON | 截断不修复；非法且未截断时有限修复 |
| stream | 提交后中断 | 不重试，发 error 帧，trace=failed |
| internal | 未预期异常 | 500，trace=failed |
