# 04 验证计划

`scripts/verify_all.py` 启动时解析本文件中的全部 `V-XXX-NN` 编号集合，与脚本注册的检查集合比对：缺项、多项都判 FAIL。

## 1. 环境与注入

- 同一进程内用 `uvicorn.Server` 以真实 socket（随机端口）启动 mock 上游与网关；网关配置为 `config/gateway.verify.yaml`（`__MOCK_BASE__`/`__TMP__` 由脚本替换）。
- `create_app(settings, clock=..., sleep=..., rng=...)` 注入：`OffsetClock`（真实单调时间 + 可手动推进的偏移）、记录型假 sleep（不发等待、不推进时钟）、固定种子 rng。因此退避序列可断言且整套验证真实耗时 < 90s。
- 另起一个 base_url 指向已关闭端口的网关实例，用于连接拒绝类错误。
- mock 控制面：`POST /__control`（按协议设置场景，支持按调用序号序列）、`GET /__stats`（调用次数、最近请求头/体、活跃连接数、客户端断开观察、校验违规）、`POST /__reset`。
- 哨兵：注入唯一哨兵 API Key 与哨兵提示词变量，验证结束后全量检索响应、日志、trace 库、模板库。
- 依赖 trace 的检查在 M7 之前可标 `DEFERRED`；`--upto` 过滤里程碑；`--only` 按编号前缀过滤；`--live` 仅跑 live 项。

## 2. 分层与轮次

| 轮次 | 层 | 功能 |
| --- | --- | --- |
| R1 | 内部协议 + 模拟适配器 | F1、F7 信封与入口校验 |
| R2 | 真实适配器结构 | F2 请求契约、配置读取、超时映射、供应商标识 |
| R3 | 流式与取消 | F3 内部事件→SSE、客户端断开 |
| R4 | 结构化输出 | F4 |
| R5 | 路由、重试、限流 | F1、F7、F8、F9 |
| R6 | 提示词模板 | F5 |
| R7 | 资源管理与可观测闭环 | F6 |

## 3. 检查清单

### 3.1 F1 统一抽象层与路由

| 编号 | 里程碑 | 检查内容 |
| --- | --- | --- |
| V-ROUTE-01 | M2 | pro/flash 分别只命中对应 mock，另一协议计数为 0 |
| V-ROUTE-02 | M1 | 未知 model → 404 model_not_found，零上游，details 含可用模型 |
| V-ROUTE-03 | M2 | 临时配置新增第三个逻辑模型（复用协议），不改代码路由成功 |
| V-ROUTE-04 | M8 | 静态检查：src/ 无写死模型名、域名、Key 样式 |
| V-ROUTE-05 | M2 | 两模型非流式响应字段集合完全一致 |
| V-ROUTE-06 | M1 | 未知字段 422；客户端 Authorization/自定义头不出现在上游请求 |
| V-ROUTE-07 | M1 | stream 能力不足 → 400 unsupported_capability，零上游 |

### 3.2 F2 适配器与 Usage 归一

| 编号 | 里程碑 | 检查内容 |
| --- | --- | --- |
| V-ADP-01 | M2 | pro 上游请求契约：Bearer、instructions/input/model/max_output_tokens，无 messages/system/max_tokens/store |
| V-ADP-02 | M2 | flash 上游请求契约：x-api-key、顶层 system、messages 无 system 且首条 user、max_tokens |
| V-ADP-03 | M2 | 多条 system 正确合并；flash 相邻同角色 user 合并 |
| V-ADP-04 | M2 | stop/length/content_filter 表驱动，两协议归一一致 |
| V-ADP-05 | M2 | Usage 归一：Responses 100/30/50/20→total 150；Anthropic 70+30→input 100、total 150 |
| V-ADP-06 | M2 | 上游缺 usage → 全字段 null 且 usage_missing=true |
| V-ADP-07 | M8 | 完整跑完后响应/头/日志/trace 中无哨兵 Key |
| V-ADP-08 | M8 | live 冒烟：两模型各一次非流式 + 流式（`--live` 且需真实 Key） |

### 3.3 F3 流式

| 编号 | 里程碑 | 检查内容 |
| --- | --- | --- |
| V-STREAM-01 | M4 | 非缓冲：mock 每 100ms 一块共 5 块，首块早于最后发送时间，间隔近似 |
| V-STREAM-02 | M4 | 首块 role、中间 content、末块 finish_reason+usage、[DONE]；拼接等于非流式 |
| V-STREAM-03 | M4 | pro 与 flash 都通过 01–02 |
| V-STREAM-04 | M4 | 客户端流不出现 response.* / content_block_* 事件名 |
| V-STREAM-05 | M4 | 解析鲁棒：1 字节切分、粘包、中文多字节、CRLF、注释行、ping |
| V-STREAM-06 | M4 | 提交前失败可重试：503,503,200 → 正常流，retry_count=2，调用 3 次 |
| V-STREAM-07 | M4 | 提交前 401 → 普通 JSON 错误，非 SSE，调用 1 次 |
| V-STREAM-08 | M4 | 提交后中断 → error 帧结束，调用 1 次，trace=failed 且已发字符 > 0 |
| V-STREAM-09 | M4 | 客户端断开 → ≤2s mock 观察到断开，trace=cancelled，活跃连接归零 |
| V-STREAM-10 | M4 | 两协议无终止事件 EOF → stream_interrupted |
| V-STREAM-11 | M4 | 心跳：静默超过 0.3s 收到 keep-alive 注释 |
| V-STREAM-12 | M4 | stream 与结构化同用 → 400，零上游 |
| V-STREAM-13 | M4 | 首块延迟 300ms → trace.ttft_ms ∈ [300,450] 且 < total_ms |
| V-STREAM-14 | M4 | 流式 usage 等于终止事件；Anthropic message_start 与 message_delta 未相加 |

### 3.4 F4 结构化输出

| 编号 | 里程碑 | 检查内容 |
| --- | --- | --- |
| V-STRUCT-01 | M5 | json_object 两模型成功，parsed 为对象，content 可 json.loads |
| V-STRUCT-02 | M5 | json_schema 两模型成功，独立 jsonschema 复验 |
| V-STRUCT-03 | M5 | pro 有 text.format；flash 无原生结构化字段且 system 含 Schema 与「JSON」 |
| V-STRUCT-04 | M5 | 语法错误修复一次成功；修复请求含错误位置与类型、不含内部策略；usage 为两次之和 |
| V-STRUCT-05 | M5 | Schema 违反（缺字段/类型错）触发修复并成功 |
| V-STRUCT-06 | M5 | 连续 3 次非法 → 502 structured_output_invalid，调用恰 3 次，无成功形态 |
| V-STRUCT-07 | M5 | finish_reason=length 且 JSON 不完整 → structured_output_truncated，调用 1 次 |
| V-STRUCT-08 | M5 | markdown 围栏 JSON 不被静默接受，触发修复 |
| V-STRUCT-09 | M5 | json_object 顶层数组视为非法（触发修复） |
| V-STRUCT-10 | M5 | Schema 本身非法 → 422 invalid_response_format，零上游 |
| V-STRUCT-11 | M5 | 预算独立：503,503,非法,合法 → retry_count=2、repair_count=1 |

### 3.5 F5 提示词模板

| 编号 | 里程碑 | 检查内容 |
| --- | --- | --- |
| V-TPL-01 | M6 | 创建 201；重复同 name+version → 409 template_conflict |
| V-TPL-02 | M6 | 按 name+version 调用，上游消息与独立渲染逐字一致（两模型） |
| V-TPL-03 | M6 | 1.0.0/1.1.0 渲染不同；缺省与 latest 均取最高版本且响应/trace 记录实际版本 |
| V-TPL-04 | M6 | 缺必填变量 422，零上游；有默认值可省略 |
| V-TPL-05 | M6 | 多传未声明、传对象/列表、超长 → 422 template_render_error |
| V-TPL-06 | M6 | 模板/版本不存在 → 404 template_not_found |
| V-TPL-07 | M6 | 变量含 {{ other }} 或注入文本原样出现，不二次渲染、不成为 system |
| V-TPL-08 | M6 | prompt+messages 且 messages 含 system → 422 |
| V-TPL-09 | M6 | 创建静态检查：占位符未声明、声明未使用、role 非法、版本非 SemVer |
| V-TPL-10 | M6 | 重启后模板仍在；PUT/DELETE 405 |
| V-TPL-11 | M6 | trace 记录 prompt_name/version/指纹 |
| V-TPL-12 | M6 | render 预览不调用上游 |
| V-TPL-13 | M6 | 模板可与 stream、response_format 组合 |
| V-TPL-14 | M6 | 种子导入幂等；同版本内容被改 → 启动报错退出 |

### 3.6 F6 可观测性

| 编号 | 里程碑 | 检查内容 |
| --- | --- | --- |
| V-OBS-01 | M7 | 成功/失败/拒绝/取消各恰一条 trace，request_id 与响应头一致 |
| V-OBS-02 | M7 | 两协议 token 分类逐项与期望相等 |
| V-OBS-03 | M7 | 非流式延迟 400ms → total≥400、ttft=null；流式 300ms → ttft 区间且 generation=total−ttft |
| V-OBS-04 | M7 | 失败两次后成功：retry_count=2、attempts 长 3、retry_wait_ms 等于假 sleep 之和 |
| V-OBS-05 | M7 | 失败/拒绝 trace 的 error_code/stage 与响应一致 |
| V-OBS-06 | M7 | trace 记录逻辑模型、上游模型、协议、模板名与版本 |
| V-OBS-07 | M7 | traces 过滤、单条查询、stats 求和对账、成功率一致 |
| V-OBS-08 | M7 | 隐私：trace/日志无完整提示词、完整输出、哨兵 Key |
| V-OBS-09 | M7 | AST 静态检查：业务层零 import observability |
| V-OBS-10 | M7 | 关闭插件：admin 路由 404，核心链路仍可用 |
| V-OBS-11 | M7 | 故障隔离：订阅者抛异常/队列满/存储不可写，请求成功且额外延迟 <50ms，dropped 可见 |
| V-OBS-12 | M7 | 重启后历史 trace 仍可查 |

### 3.7 F7 错误体系

| 编号 | 里程碑 | 检查内容 |
| --- | --- | --- |
| V-ERR-01 | M2 | 表驱动状态码/异常映射，两协议断言 code/HTTP/stage/retryable，可重试 4 次、不可重试 1 次 |
| V-ERR-02 | M1 | 所有错误信封字段齐全，X-Request-ID 与 error.request_id 一致 |
| V-ERR-03 | M2 | 不泄露：错误体/日志无哨兵 Key、鉴权头、堆栈、上游全文（摘要 ≤300） |
| V-ERR-04 | M1 | 请求级校验错误 → 422/400，零上游 |
| V-ERR-05 | M1 | 适配器未预期异常 → 500 internal_error，信封一致且有 trace |
| V-ERR-06 | M2 | 200 非 JSON/缺 output → protocol_error；200 空文本 → empty_response |
| V-ERR-07 | M3 | 上游 429 → upstream_rate_limited(503)；网关限流 → rate_limit_exceeded(429) |

### 3.8 F8 重试

| 编号 | 里程碑 | 检查内容 |
| --- | --- | --- |
| V-RETRY-01 | M3 | 持续 503 → 调用 4 次，503 upstream_overloaded，retry_count=3 |
| V-RETRY-02 | M3 | jitter=none/base0.5/factor2 → 假 sleep [0.5,1.0,2.0]；full 在 [0,d_n] |
| V-RETRY-03 | M3 | max_delay 封顶：base3/factor4/max8 → [3,8,8] |
| V-RETRY-04 | M3 | 不可重试错误 → 调用恰 1 次 |
| V-RETRY-05 | M3 | 503,503,200 → 成功，retry_count=2，调用 3 次 |
| V-RETRY-06 | M3 | Retry-After=2 → 假 sleep ≥2；Retry-After 超 max_delay → 不重试 1 次 |
| V-RETRY-07 | M3 | 超时与连接拒绝可重试，次数同 01 |
| V-RETRY-08 | M3 | total_s 耗尽 → 调用次数 <4 |
| V-RETRY-09 | M7 | mock 调用次数 == trace.attempts 长度 |
| V-RETRY-10 | M4 | 流式提交点与结构化修复预算边界（引用 06/07/08、STRUCT-11） |
| V-RETRY-11 | M8 | 退避相关检查真实总耗时 <5s |

### 3.9 F9 限流

| 编号 | 里程碑 | 检查内容 |
| --- | --- | --- |
| V-RL-01 | M3 | burst=3，5 并发 → 3 成功 2 个 429，上游 3 次 |
| V-RL-02 | M3 | 429 code/Retry-After 整数≥1/details 完整 |
| V-RL-03 | M3 | 模型独立：一侧耗尽不影响另一侧 |
| V-RL-04 | M3 | 推进 1/rate 秒后放行一个 |
| V-RL-05 | M3 | 流式同样受限，拒绝时不建立上游连接 |
| V-RL-06 | M3 | 200 并发打 burst=50 桶，通过恰 50 |
| V-RL-07 | M7 | 拒绝请求 trace=rejected 且 attempts 为空 |
| V-RL-08 | M3 | 一次客户端请求只消耗一个令牌（3 次重试不多扣） |

## 4. 报告

- 控制台表格 + `verification_report.md` + `verification_report.json`：编号 | 检查 | PASS/FAIL/SKIP/DEFERRED | 证据（关键数字）。
- 末尾「六大能力验收汇总」：模型调用、流式、结构化、模板、可观测、重试与限流，对应编号全 PASS 才 OK。
- 退出码：任一 FAIL → 1；DEFERRED 在最终报告视为未通过（`--upto M7` 之前允许）。
- 默认包含 `V-ADP-08`（live 冒烟）；缺少真实 Key 时脚本启动即失败（退出码 2）；`--no-live` 可降级为纯 mock（91 项）。
- 完整性：注册集合必须与本文件编号集合一致。
