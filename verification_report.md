# LLM Gateway 验证报告

- 命令: `scripts\verify_all.py --verbose`
- 时间: 2026-11-24 01:15:04
- 范围: upto=M8 only=全部

## 检查明细

| 编号 | 里程碑 | 检查内容 | 结果 | 证据（关键数字） |
| --- | --- | --- | --- | --- |
| V-ADP-01 | M2 | pro 上游请求契约 | PASS | {"authorization": "Bearer <key>", "fields": ["input", "instructions", "max_output_tokens", "model", "reasoning", "stream"], "instructions": "S1"} |
| V-ADP-02 | M2 | flash 上游请求契约 | PASS | {"x-api-key": "present", "system": "S1", "max_tokens": 128} |
| V-ADP-03 | M2 | system 合并与相邻同角色合并 | PASS | {"pro_instructions": "A\\nB", "flash_system": "A\\nB", "flash_user": "U1\\n\\nU2"} |
| V-ADP-04 | M2 | finish_reason 归一（stop/length/content_filter） | PASS | {"stop": {"pro": "stop", "flash": "stop"}, "length": {"pro": "length", "flash": "length"}, "content_filter": {"pro": "content_filter", "flash": "content_filter"}} |
| V-ADP-05 | M2 | Usage 归一（Responses 与 Anthropic） | PASS | {"pro": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "prompt_tokens_details": {"cached_tokens": 30}, "completion_tokens_details": {"reasoning_tokens": 20}}, "flash": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "prompt_tokens_details": {"cached_tokens": 30}, "completion_tokens_details": {"reasoning_tokens": null}}} |
| V-ADP-06 | M2 | 上游缺 usage → 字段 null | PASS | {"pro": {"prompt_tokens": null, "completion_tokens": null, "total_tokens": null, "prompt_tokens_details": {"cached_tokens": null}, "completion_tokens_details": {"reasoning_tokens": null}}, "flash": {"prompt_tokens": null, "completion_tokens": null, "total_tokens": null, "prompt_tokens_details": {"cached_tokens": null}, "completion_tokens_details": {"reasoning_tokens": null}}, "trace_usage_missing" |
| V-ADP-07 | M8 | 哨兵 Key 不出现在响应/头/日志/trace | PASS | {"captured_responses": 404, "leaks": 0} |
| V-ADP-08 | M8 | live 冒烟：两模型各一次非流式 + 流式 | PASS | {"models": {"deepseek-v4-pro": {"nonstream_total_tokens": 18, "stream_total_tokens": 37, "nonstream_chars": 33, "stream_chars": 117}, "deepseek-v4-flash": {"nonstream_total_tokens": 20, "stream_total_tokens": 20, "nonstream_chars": 40, "stream_chars": 40}}, "subchecks": [{"name": "deepseek-v4-pro 非流式", "status": "OK", "detail": "total_tokens=18"}, {"name": "deepseek-v4-pro 流式", "status": "OK", "de |
| V-ERR-01 | M2 | 错误映射表驱动（两协议） | PASS | {"responses_http": "ok", "anthropic_http": "ok", "responses_body": "ok", "anthropic_body": "ok", "responses_timeout": "ok", "anthropic_timeout": "ok", "connection_reset_calls": 4} |
| V-ERR-02 | M1 | 错误信封字段齐全且 X-Request-ID 一致 | PASS | {"fields": ["code", "details", "message", "request_id", "retryable", "stage", "type"], "request_id": "a8791d1341614c06a5b65f69ecb2a5dc"} |
| V-ERR-03 | M2 | 不泄露 Key/鉴权头/上游全文（摘要 ≤300） | PASS | {"message_length": 61, "contains_key": false} |
| V-ERR-04 | M1 | 请求级校验错误 422/400，零上游 | PASS | {"missing_model": {"http": 422, "code": "invalid_request"}, "empty_messages": {"http": 422, "code": "invalid_request"}, "temperature": {"http": 422, "code": "invalid_request"}, "unknown_field": {"http": 422, "code": "invalid_request"}} |
| V-ERR-05 | M1 | 适配器未预期异常 → 500 internal_error，信封一致 | PASS | {"status": 500, "code": "internal_error", "request_id": "05e23cfb1ac443aaa57c70f63f140455"} |
| V-ERR-06 | M2 | 200 非 JSON/缺字段/空文本分别映射 | PASS | {"deepseek-v4-pro:non_json": "upstream_protocol_error", "deepseek-v4-pro:missing_output": "upstream_protocol_error", "deepseek-v4-pro:empty_text": "upstream_empty_response", "deepseek-v4-flash:non_json": "upstream_protocol_error", "deepseek-v4-flash:missing_output": "upstream_protocol_error", "deepseek-v4-flash:empty_text": "upstream_empty_response"} |
| V-ERR-07 | M3 | 上游 429 → 503 upstream_rate_limited；网关限流才是 429 | PASS | {"upstream": {"http": 503, "code": "upstream_rate_limited"}, "gateway": {"http": 429, "code": "rate_limit_exceeded"}} |
| V-OBS-01 | M7 | 成功/失败/拒绝/取消各恰一条 trace | PASS | {"completed": "dde0711f00024f2ebbe95f02d24c7434", "failed": "cce407f1d05a458793edc06b916fdcc9", "rejected": "c4e9c8bb10b54c20b4a4c460e852b51f", "cancelled": "99075253f7ef4b149e6f82e195c54995"} |
| V-OBS-02 | M7 | Token 分类（两协议） | PASS | {"pro": {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 30, "reasoning_tokens": 20, "total_tokens": 150}, "flash": {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 30, "cache_creation_tokens": 0, "total_tokens": 150}} |
| V-OBS-03 | M7 | 延迟拆分：非流式 total≥400、ttft=null；流式 ttft<total | PASS | {"non_stream": {"total_ms": 407.7}, "stream": {"ttft_ms": 334.7, "total_ms": 335.2, "generation_ms": 0.5}} |
| V-OBS-04 | M7 | 重试被记录：attempts 长度与退避总和 | PASS | {"retry_count": 2, "attempts": 3, "retry_wait_ms": 1500.0, "sleeps": [0.5, 1.0]} |
| V-OBS-05 | M7 | 失败与拒绝 trace 的 error_code/stage 与响应一致 | PASS | {"failed": {"code": "upstream_server_error", "stage": "upstream_http"}, "rejected": {"status": "rejected", "code": "rate_limit_exceeded"}} |
| V-OBS-06 | M7 | trace 记录逻辑模型、上游模型、协议、模板名与版本 | PASS | {"logical_model": "deepseek-v4-flash", "upstream_model": "deepseek-v4-flash", "protocol": "anthropic_messages", "prompt": {"name": "code-review", "version": "1.1.0"}} |
| V-OBS-07 | M7 | traces 过滤/单条查询/stats 对账 | PASS | {"traces": 84, "flash_rows": 28, "models": ["deepseek-v4-flash", "deepseek-v4-pro", "missing-model", "rl-alpha", "timeout-flash", "timeout-pro", "unknown"]} |
| V-OBS-08 | M7 | 隐私：trace/日志无完整提示词、完整输出、哨兵 Key | PASS | {"trace_fields_only": true, "prompt_chars": 35, "fingerprint": "09a903cdd71452d5"} |
| V-OBS-09 | M7 | 解耦静态检查：业务层零 import observability | PASS | {"business_imports": 0, "main_wires_plugin": true} |
| V-OBS-10 | M7 | 关闭插件：admin 404，核心链路仍可用 | PASS | {"admin": 404, "chat": 200, "stream": "ok", "render": 200} |
| V-OBS-11 | M7 | 故障隔离：订阅者异常/存储不可写/队列满 | PASS | {"write_errors": 5, "dropped": 4, "subscriber_errors": 21, "overhead_ms": 0.15} |
| V-OBS-12 | M7 | 重启后历史 trace 仍可查 | PASS | {"history_rows": 5, "single_query": 200} |
| V-RETRY-01 | M3 | 持续 503 → 4 次调用、3 次退避、最终 503 | PASS | {"calls": 4, "sleeps": [0.5, 1.0, 2.0], "code": "upstream_overloaded"} |
| V-RETRY-02 | M3 | 指数退避序列与 full jitter 范围 | PASS | {"no_jitter": [0.5, 1.0, 2.0], "full_jitter": [0.16191638241658118, 0.15084917392450192, 1.3018689460797075]} |
| V-RETRY-03 | M3 | max_delay 封顶 [3,8,8] | PASS | {"sleeps": [3.0, 8.0, 8.0]} |
| V-RETRY-04 | M3 | 不可重试错误只调用 1 次 | PASS | {"400": 1, "401": 1, "402": 1, "403": 1, "422": 1, "protocol": 1, "truncated": 1} |
| V-RETRY-05 | M3 | 503,503,200 → 成功，retry_count=2，调用 3 次 | PASS | {"calls": 3, "retry_count": 2, "sleeps": [0.5, 1.0]} |
| V-RETRY-06 | M3 | Retry-After 参与退避；超过 max_delay 不重试 | PASS | {"retry_after_sleep": [2.0], "huge_retry_after_calls": 1} |
| V-RETRY-07 | M3 | 超时与连接重置同样可重试 4 次 | PASS | {"timeout": 4, "connection_reset": 4} |
| V-RETRY-08 | M3 | total_s 预算耗尽后不再重试 | PASS | {"calls": 1, "budget_exhausted": true} |
| V-RETRY-09 | M7 | mock 调用次数 == trace.attempts 长度 | PASS | {"mock_calls": 3, "attempts": 3, "retry_count": 2} |
| V-RETRY-10 | M4 | 流式提交点与结构化修复预算边界 | PASS | {"stream_retry_count": 2, "structured_retry": 2, "repair": 1} |
| V-RETRY-11 | M8 | 退避相关检查真实总耗时 <5s | PASS | {"retry_checks_total_ms": 1367.5} |
| V-RL-01 | M3 | burst=3 时 5 并发恰 3 成功 2 个 429 | PASS | {"codes": [200, 200, 200, 429, 429], "upstream_calls": 3} |
| V-RL-02 | M3 | 429 信封、Retry-After 与 details | PASS | {"retry_after": 1, "details": {"model": "rl-alpha", "rate_per_s": 1.0, "burst": 3, "retry_after_s": 1.0}} |
| V-RL-03 | M3 | 模型之间限流相互独立 | PASS | {"alpha_exhausted": 3, "beta_ok": 200, "beta_exhausted": 3, "alpha_ok": 200} |
| V-RL-04 | M3 | 假时钟推进 1/rate 后放行一个 | PASS | {"before": 429, "after_advance": 200} |
| V-RL-05 | M3 | 流式同样受限，拒绝时不建立上游连接 | PASS | {"stream_status": 429, "upstream_calls_before": 3, "after": 3} |
| V-RL-06 | M3 | 200 并发打 burst=50：恰 50 通过 | PASS | {"passed": 50, "rejected": 150} |
| V-RL-07 | M7 | 被限流请求 trace=rejected 且不进入重试 | PASS | {"status": "rejected", "attempts": 0, "error_code": "rate_limit_exceeded"} |
| V-RL-08 | M3 | 一次客户端请求只消耗一个令牌 | PASS | {"successful_requests": 3, "upstream_calls": 6, "fourth_request": 429} |
| V-ROUTE-01 | M2 | pro/flash 分别只命中对应 mock | PASS | {"pro": {"responses": 1, "anthropic": 0}, "flash": {"responses": 0, "anthropic": 1}} |
| V-ROUTE-02 | M1 | 未知 model → 404 model_not_found，零上游 | PASS | {"status": 404, "code": "model_not_found", "available": ["deepseek-v4-flash", "deepseek-v4-pro", "no-stream-pro", "rl-alpha", "rl-beta", "rl-big", "timeout-flash", "timeout-pro", "tiny-budget-pro"], "upstream_calls": 0} |
| V-ROUTE-03 | M2 | 临时配置新增第三个逻辑模型即可路由 | PASS | {"model": "deepseek-v4-pro-2", "http": 200, "code_changes": 0} |
| V-ROUTE-04 | M8 | src 无写死模型名、域名、Key 样式 | PASS | {"files": 36, "violations": 0} |
| V-ROUTE-05 | M2 | 两模型非流式响应字段集合完全一致 | PASS | {"shape": {"top": ["choices", "created", "gateway", "id", "model", "object", "usage"], "choice": ["finish_reason", "index", "message"], "message": ["content", "role"], "usage": ["completion_tokens", "completion_tokens_details", "prompt_tokens", "prompt_tokens_details", "total_tokens"], "usage_prompt_details": ["cached_tokens"], "usage_completion_details": ["reasoning_tokens"], "gateway": ["latency |
| V-ROUTE-06 | M1 | 未知字段 422；客户端头不转发 | PASS | {"unknown_field": 422, "forwarded_client_headers": 0, "upstream_auth": "gateway_key"} |
| V-ROUTE-07 | M1 | stream 能力不足 → 400 unsupported_capability，零上游 | PASS | {"status": 400, "code": "unsupported_capability", "upstream_calls": 0} |
| V-STREAM-01 | M4 | 流式非缓冲：首块早于上游末块，间隔近似 | PASS | {"chunks": 5, "span_s": 0.439, "intervals": [0.109, 0.109, 0.108, 0.113]} |
| V-STREAM-02 | M4 | SSE 结构完整且拼接等于非流式 | PASS | {"streamed": "Hello from mock", "non_stream": "Hello from mock"} |
| V-STREAM-03 | M4 | pro 与 flash 都通过流式基础检查 | PASS | {"pro": {"chunks": 5, "span_s": 0.438, "intervals": [0.11, 0.11, 0.109, 0.109]}, "flash": {"chunks": 5, "span_s": 0.436, "intervals": [0.107, 0.109, 0.109, 0.11]}} |
| V-STREAM-04 | M4 | 供应商事件名不泄漏 | PASS | {"deepseek-v4-pro": "clean", "deepseek-v4-flash": "clean"} |
| V-STREAM-05 | M4 | SSE 解析鲁棒（单测式） | PASS | {"chunk_sizes": [1, 2, 3, 80], "events": 3} |
| V-STREAM-06 | M4 | 提交前失败可重试：503,503,200 → retry_count=2 | PASS | {"retry_count": 2, "calls": 3, "sleeps": [0.5, 1.0]} |
| V-STREAM-07 | M4 | 提交前 401 → 普通 JSON 错误，调用 1 次 | PASS | {"http": 502, "code": "upstream_auth_error", "calls": 1} |
| V-STREAM-08 | M4 | 提交后中断 → error 帧结束，调用 1 次 | PASS | {"error_code": "stream_interrupted", "calls": 1, "emitted_chars": 2} |
| V-STREAM-09 | M4 | 客户端断开 ≤2s 观察上游关闭，活跃连接归零 | PASS | {"disconnect_observed_s": 0.083, "active": 0, "trace_status": "cancelled"} |
| V-STREAM-10 | M4 | 两协议无终止事件 EOF → stream_interrupted | PASS | {"deepseek-v4-pro": "stream_interrupted", "deepseek-v4-flash": "stream_interrupted"} |
| V-STREAM-11 | M4 | 心跳：静默期收到 keep-alive | PASS | {"keepalive_frames": 2} |
| V-STREAM-12 | M4 | stream 与结构化互斥 → 400，零上游 | PASS | {"status": 400, "code": "unsupported_parameter_combination", "upstream_calls": 0} |
| V-STREAM-13 | M4 | TTFT：首块延迟 300ms → trace.ttft_ms 区间且 < total | PASS | {"ttft_ms": 309.6, "total_ms": 310.0, "generation_ms": 0.4} |
| V-STREAM-14 | M4 | 流式 usage 取终止事件，Anthropic 未相加 | PASS | {"pro": {"prompt": 100, "completion": 50, "total": 150}, "flash": {"prompt": 100, "completion": 50, "total": 150}} |
| V-STRUCT-01 | M5 | json_object 两模型成功，parsed 为对象 | PASS | {"deepseek-v4-pro": {"name": "alpha", "n": 1}, "deepseek-v4-flash": {"name": "alpha", "n": 1}} |
| V-STRUCT-02 | M5 | json_schema 两模型成功且独立复验 | PASS | {"deepseek-v4-pro": {"a": 7}, "deepseek-v4-flash": {"a": 7}} |
| V-STRUCT-03 | M5 | pro 原生 text.format；flash 提示词注入 Schema | PASS | {"pro_format": "json_schema", "flash_system_has_schema": true} |
| V-STRUCT-04 | M5 | 语法错误修复一次成功，usage 累加 | PASS | {"repair_count": 1, "calls": 2, "usage_total": 30} |
| V-STRUCT-05 | M5 | Schema 违反（缺字段/类型错）触发修复 | PASS | {"repair_count": 1, "parsed": {"a": 2}} |
| V-STRUCT-06 | M5 | 修复预算耗尽 → 502 structured_output_invalid | PASS | {"http": 502, "code": "structured_output_invalid", "calls": 3, "repair_count": 2} |
| V-STRUCT-07 | M5 | 截断不修复 → structured_output_truncated | PASS | {"http": 502, "code": "structured_output_truncated", "calls": 1} |
| V-STRUCT-08 | M5 | markdown 围栏 JSON 不被静默接受 | PASS | {"repair_count": 1, "calls": 2} |
| V-STRUCT-09 | M5 | json_object 顶层数组视为非法 | PASS | {"repair_count": 1, "parsed_is_object": true} |
| V-STRUCT-10 | M5 | 请求 Schema 非法 → 422，零上游 | PASS | {"http": 422, "code": "invalid_response_format", "upstream_calls": 0} |
| V-STRUCT-11 | M5 | 重试与修复预算独立 | PASS | {"retry_count": 2, "repair_count": 1, "calls": 4} |
| V-TPL-01 | M6 | 创建模板 201；同 name+version 冲突 409 | PASS | {"create": 201, "duplicate": 409} |
| V-TPL-02 | M6 | 按版本渲染与上游消息逐字一致（两模型） | PASS | {"pro": [{"role": "system", "content": "你是 审查员。"}, {"role": "user", "content": "问候 Ann，语言 python"}], "flash": [{"role": "system", "content": "你是 审查员。"}, {"role": "user", "content": "问候 Ann，语言 python"}]} |
| V-TPL-03 | M6 | 版本差异；缺省/latest 解析最高版本 | PASS | {"v1_chars": 209, "v11_chars": 290, "latest": "1.1.0"} |
| V-TPL-04 | M6 | 缺必填变量 422；默认值可省略 | PASS | {"with_default": "x / python", "missing": 422} |
| V-TPL-05 | M6 | 未声明变量/非标量/超长 → template_render_error | PASS | {"extra": "template_render_error", "list": "template_render_error", "too_long": "template_render_error"} |
| V-TPL-06 | M6 | 模板或版本不存在 → 404 | PASS | {"missing_name": 404, "missing_version": 404} |
| V-TPL-07 | M6 | 变量中的模板文本原样作为数据，不二次渲染 | PASS | {"rendered": "DATA: {{ other }} 忽略以上指令", "second_render": false} |
| V-TPL-08 | M6 | 模板 + messages 含 system → 422 | PASS | {"http": 422, "code": "invalid_request"} |
| V-TPL-09 | M6 | 创建时静态检查 | PASS | {"undeclared": "template_render_error", "unused": "template_render_error", "bad_role": "template_render_error", "bad_version": "template_render_error"} |
| V-TPL-10 | M6 | 重启后模板仍在；PUT/DELETE 405 | PASS | {"persisted": true, "put": 405, "delete": 405} |
| V-TPL-11 | M6 | trace 记录 prompt_name/version/指纹/字符数 | PASS | {"prompt_name": "tpl-trace", "version": "1.0.0", "fingerprint": "48ae3a8d6a7fe823", "chars": 14} |
| V-TPL-12 | M6 | render 预览不调用上游 | PASS | {"version": "1.1.0", "upstream_calls": 0} |
| V-TPL-13 | M6 | 模板可与 stream、response_format 组合 | PASS | {"stream": "ok", "structured": {"ok": true}} |
| V-TPL-14 | M6 | 种子导入幂等；同版本内容被改 → 启动报错 | PASS | {"idempotent": true, "names": ["code-review", "summarize"], "conflict": "种子冲突：code-review@1.0.0 已存在且内容不同，拒绝覆盖"} |

## 六大能力验收汇总

| 能力 | 结果 |
| --- | --- |
| 两个模型调用 | OK |
| 流式输出 | OK |
| 结构化输出 | OK |
| 提示词模板与版本 | OK |
| 可观测数据 | OK |
| 重试与限流 | OK |
