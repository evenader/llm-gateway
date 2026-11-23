# 05 决策与假设

## 1. 继承提示词的默认假设

| 编号 | 决策 |
| --- | --- |
| A1 | 对外入口采用 OpenAI Chat Completions 风格，两个上游各自翻译 |
| A2 | 「最多 3 次」= 最多 3 次重试（最多 4 次上游调用），可配置 |
| A3 | stream 与 response_format≠text 互斥，入口 400 |
| A4 | 不做 fallback / 动态路由 / 工具调用 / 多模态 / 会话存储；`RouteDecision.reason` 预留 |
| A5 | SQLite；trace 与模板分库，插件与业务不共用连接 |
| A6 | trace 不存提示词与输出正文，只存指纹与字符数 |
| A7 | 网关鉴权默认关闭 |
| A8 | TTFT = 网关收到请求 → 上游首个非空文本 delta；非流式 null |
| A9 | 每逻辑模型一个令牌桶，按请求计数，进程内 |
| A10 | thinking 默认关闭，推理内容不对外下发 |
| A11 | 端点与模型名以配置为准（DeepSeek 官方文档 2026-11 核对值写在 gateway.yaml） |
| A12 | flash 无原生结构化 → 提示词注入 Schema + 本地校验 + 有限修复；pro 原生同样本地复验 |
| A13 | 上游缺 usage 不估算 |

## 2. 实现决策

| 编号 | 决策与理由 |
| --- | --- |
| D01 | 项目根目录 = `course_code/homework/homework01`，源码在其 `src/gateway/`，与提示词文档的 `doc/` 并列 |
| D02 | 对外 schema（api/schemas.py）与内部类型（core/types.py）完全分离，适配器只认内部类型 |
| D03 | `/v1/chat/completions` 路由直接读取 body 并在 `ChatService` 内做 pydantic 校验，保证请求级 422 也走统一 trace 发射点（每个 chat 请求恰好一条 trace） |
| D04 | 重试/限流用注入的 `Clock`；延迟与 TTFT 用真实 `perf_counter`，避免验证时假时钟破坏延迟断言 |
| D05 | 验证配置用 `__MOCK_BASE__`、`__TMP__` 占位，verify_all 替换后写入临时 yaml；生产配置不含占位 |
| D06 | 验证配置声明若干验证专用逻辑模型（低 burst 限流、stream=false、超小 total_s），全部通过配置实现，代码零分支 |
| D07 | 修复指令格式：`上一次输出未通过 JSON 校验。错误：<path>: <type>... 只修正出错的语法和字段，其余保持不变，只输出 JSON，不要代码围栏。` 不包含网关内部策略 |
| D08 | 模板占位符语法 `{{ name }}`，正则单次替换；Anthropic 相邻同角色消息用 `\n\n` 合并，多条 system 用 `\n` 合并 |
| D09 | 流式实现：后台泵任务把适配器事件放入 `asyncio.Queue`，SSE 编码器 `wait_for` 取事件实现心跳；取消时停泵并关闭上游 |
| D10 | 流式请求的 trace 在 SSE 生成器结束时 finalize（completed/failed/cancelled），`finalize` 幂等 |
| D11 | 结构化修复把「原消息 + assistant(非法输出) + user(修正指令)」作为新一轮内部消息，每轮独立套用 RetryPolicy |
| D12 | 结构化模式下 `finish_reason=length` 且校验失败才判 `structured_output_truncated`；若截断但 JSON 恰好合法则按成功 |
| D13 | `unsupported_capability` 覆盖 `stream=false` 与 `structured_output=false`；`native_json_*` 只决定结构化路径 |
| D14 | 网关自身 429 才返回 `Retry-After`；上游 429 一律 503 `upstream_rate_limited` |
| D15 | 管理端点查询不存在 trace 用 404 `trace_not_found`（提示词表未定义，属管理面附加码，已回写 03） |
| D16 | `created` 用收到请求时的 epoch 秒；`latency_ms` 用真实单调时钟测得的毫秒（保留 1 位小数） |
| D17 | Reorder：入口校验 → 路由 → 限流 → 模板 → 能力 → 上游；被限流请求不渲染模板 |
| D18 | mock 场景支持 `sequence`（按调用序号的场景列表），用于重试与修复循环 |
| D19 | 记录 `sanitize` 摘要 ≤300 字符；上游错误体只解析 message，不落全文 |
| D20 | 验证中连接拒绝用一个 base_url 指向关闭端口的第二网关实例，不改生产代码 |
| D21 | V-ERR-05 通过在 verify_all 内替换某个模型的 provider 为「抛未预期异常」的测试适配器触发，验证通用异常路径 |
| D22 | `verify_all.py` 以 `--upto` 支持里程碑门禁；最终运行不得含 DEFERRED |
| D23 | 心跳间隔生产 15s、验证 0.3s；心跳只是 SSE 注释行，不产生 chunk 事件 |
| D24 | 速率桶初始满桶；Retry-After 计算 `ceil(缺口/rate)`，至少 1 秒 |
| D25 | 非流式响应中的 `usage_missing` 不进入对外 usage，仅 trace 记录；对外缺失字段为 null |
| D26 | 流式上游请求使用独立的 `httpx.AsyncClient`（每流一个），关闭时先关响应再关 client；非流式仍复用共享连接池。理由：共享池下取消一条流时 httpx 会把半读连接放回池中，上游实际未断开；独立 client 保证取消穿透。代价是流式请求无跨请求连接复用 |
| D27 | request_id 与网关鉴权用纯 ASGI 中间件实现，不用 `BaseHTTPMiddleware`（后者会包裹 receive，干扰断连检测与流式） |
| D28 | 取消清理在独立 task 中执行（先关上游、再取消泵），避免响应生成器被取消时 await 清理被中断 |
| D29 | providers 与验证客户端均 `trust_env=False`：避免系统代理影响本地上游与连接重置测试；上游地址由配置显式给出 |
| D30 | 「连接被重置」用本地 TCP 服务器写入非法 HTTP 响应模拟；httpx 对“接受后直接关闭”会返回空 502，不足以触发连接错误分类 |
| D31 | 模板指纹 = `sha256(json.dumps(rendered_messages, ensure_ascii=False, sort_keys=True, separators=(",",":")))`，字符数为渲染后各消息 content 长度之和 |
| D32 | `V-ADP-08`（live 冒烟）默认纳入运行，真实上游验证优先；缺少真实 `DEEPSEEK_API_KEY` 时脚本启动即失败（退出码 2），不静默 SKIP；`--no-live` 可降级为纯 mock 验证；mock 默认使用哨兵 Key |
| D33 | 流式上游使用独立 client 后，非流式仍共享连接池；trace 写入为后台线程批量 `INSERT OR REPLACE`，查询走独立只读连接 |

## 3. 集中提问

无阻塞性问题。其余歧义按上述默认假设执行，如与真实上游不符只改配置。
