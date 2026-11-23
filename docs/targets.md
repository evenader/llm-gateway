# LLM Gateway 开发提示词
## 0. 角色与目标

你是资深 Python 后端工程师，要完成一个 LLM Gateway（大模型网关）。网关对外只暴露一个统一入口，对内通过适配器模式调用两个不同协议的上游：

- deepseek-v4-pro → OpenAI Responses API
- deepseek-v4-flash → Anthropic Messages API

必须实现并自证的六大能力：流式输出、结构化输出、提示词模板与版本引用、可观测数据（Token 分类统计 + 延迟 + 首 Token 延迟）、指数退避重试（最多 3 次）、按模型独立限流（超限返回 429）。

完成标志：`python scripts/verify_all.py` 全部通过，且 §7 的交付物齐全。

## 1. 工作流程（硬性约束，先读完再动手）

1. **蓝图先行**。写任何业务代码前，先在 `docs/blueprint/` 生成并保存：`00-architecture.md`（分层、模块职责、依赖方向、数据流）、`01-api-contract.md`、`02-feature-design.md`（每个功能的设计及对应验证编号）、`03-error-codes.md`、`04-verification-plan.md`、`05-decisions.md`（假设与决策记录）。蓝图是实现和评审的唯一依据，要改设计先改蓝图。
2. **提问规则**。疑问在蓝图阶段一次性集中提出，且只问「答案会改变架构或对外契约」的问题。其余歧义直接采用 §9 的默认假设（或你认为更合理的方案），写进 `05-decisions.md` 后继续，不要因小事停工。
3. **里程碑门禁**。按 §8 的顺序推进。每个里程碑结束必须：跑全量单测 + 已完成功能的验证项，全部通过后在 `docs/review-log.md` 追加一条记录。
4. **改后先停并审视**（宏观要求第 5 条）。实施阶段只要改动了已完成的代码（含重构、修 bug、为新功能改共享模块），必须先停止新增功能，做回归审视：① 重跑全量测试与已有验证项；② 对照蓝图逐个检查已有功能是否仍成立；③ 在 review-log.md 记录「改了什么 / 影响面 / 复查了哪些功能 / 结果」。审视通过才能继续。
5. **边界由你显式定义**。哪些错误可重试、哪些输出可修复、何时终止，全部写成代码里的显式规则，不依赖 SDK 的默认行为，也不交给模型自行判断。
6. **禁止事项**：

- 透明代理：不得原样转发客户端请求体或原样返回上游响应，所有字段都要经网关校验、归一化后重新构造。
- 在代码里写死模型名、base\_url、API Key。Key 只来自环境变量，且只在 providers 层内部出现。
- 用 openai / anthropic 官方 SDK 发上游请求（SDK 内部自动重试会让重试次数不可控、不可记录）；统一用 httpx.AsyncClient。
- 把可观测性代码写进业务路径（见 §3 原则 5）。
- 静默吞异常；失败时返回 200；对非法 JSON 输出做静默「自动修正」。
- 在异步路径用 time.sleep；在测试里真实等待退避时间（用注入的假时钟 / 假 sleep）。
- 把完整提示词、完整模型输出、API Key、鉴权头写入日志或 trace。

## 2. 范围

**要做**：统一入口 + 两个协议适配器 + 动态路由；流式；结构化输出（含有限次修复）；提示词模板与版本管理；可观测性插件；统一错误体系；指数退避重试；按模型限流；配置驱动；验证脚本与 README。

**不做（显式非目标，不要自行扩展）**：工具调用 / function calling、多模态、会话与 previous\_response\_id、WebSocket、动态路由与自动降级（fallback）、多租户与计费账单、前端页面。

不做降级的理由：降级模型必须在上下文长度、结构化输出、工具调用等能力上与原模型等价；pro 与 flash 能力不等价，盲目切换既不能修复拒答、截断、格式错误，还会掩盖根因。请只保留扩展点：Router.resolve() 返回 RouteDecision（含选择原因），今后加降级时不用改调用方。

## 3. 架构原则

1. **统一抽象层 + 适配器模式**。根据请求里的 model 字段路由到对应适配器，屏蔽鉴权、请求体结构、返回格式差异。两个适配器必须暴露相同能力、接受相同的内部输入、返回相同的内部输出。
2. **入口出口先稳定**。先定对外请求/响应 schema（pydantic，extra=forbid），再写适配逻辑。
3. **分层与依赖方向**：api → services → providers / core，下层不得 import 上层。
4. **供应商类型不出 provider 层**。适配器只返回内部 UnifiedResponse / StreamEvent / Usage，SDK 或供应商原始对象、事件名不得上浮到 services 和 api。
5. **可观测性插件化**。core 只通过 EventBus 发出生命周期事件，不知道谁订阅；observability 包订阅事件并落库，由组合根 main.py 装配（main.py 是唯一同时 import 业务与 observability 的地方）。删除或关闭 observability 目录，网关业务必须照常工作。订阅者抛异常、变慢、队列满都不得影响请求。
6. **配置驱动**。模型表、限流、超时、重试、模板种子全部来自配置，代码里没有具体模型名与地址。
7. **逻辑模型名**。对外 model 是配置里定义的逻辑名（如 deepseek-v4-pro），不要用「主模型 / 备用模型」这类角色词命名。
8. **先分类，再决定是否重试**。错误按发生阶段分类（见 §4.4），重试与否由分类决定。
9. **四层成功判定**：网络层（DNS / 连接 / 超时）→ HTTP 层（状态码）→ 协议层（响应能否被解析）→ 业务层（非空、未截断、通过校验）。HTTP 200 不等于成功。

**技术栈**：Python ≥ 3.11；FastAPI、uvicorn、httpx（异步）、pydantic v2、PyYAML、jsonschema；存储用标准库 sqlite3；测试用 pytest + pytest-asyncio。

**目录结构**（可微调，需在蓝图中说明）：

```
llm-gateway/
├─ README.md  pyproject.toml  .env.example
├─ config/  gateway.yaml  prompts/*.yaml
├─ docs/  blueprint/  review-log.md
├─ src/gateway/
│  ├─ main.py            组合根
│  ├─ config.py
│  ├─ api/               routes_chat  routes_prompts  routes_misc  sse  error_handlers  schemas
│  ├─ core/              types  errors  events  retry  ratelimit  clock
│  ├─ services/          chat_service  router  structured  prompts
│  ├─ providers/         base  registry  openai_responses  anthropic_messages  sse_parser  http_errors
│  └─ observability/     plugin  recorder  store  routes  stats
├─ tests/                unit/  mock_upstream/
└─ scripts/              verify_all.py  run_mock_upstream.py
```

## 4. 对外契约

### 4.1 POST /v1/chat/completions（统一入口，两个模型共用）

请求字段（未知字段一律 422 invalid\_request 并指出字段路径）：

- model：必填，逻辑模型名。
- messages：可选，元素 role ∈ system / user / assistant，content 为字符串。
- prompt：可选，{name, version?, variables}；version 缺省或 latest = 该模板最高版本。
- stream：默认 false。
- temperature \[0,2\]、top\_p (0,1\]、max\_tokens ≥ 1、stop（字符串列表，≤ 16 项）：均可选，缺省用模型配置的默认值。
- response\_format：可选，{type: text | json\_object | json\_schema, json\_schema: {name, schema, strict}}。

组合规则：messages 与 prompt 至少给一个；两者同时给出时，模板渲染出的消息在前、messages 追加在后，且此时 messages 不得含 system 角色；stream=true 与 response\_format.type≠text 互斥，返回 400 unsupported\_parameter\_combination（流式是增量文本，结构化需要完整 JSON 才能解析，二者无法同时成立，要在入口提前报错）。

非流式响应：id（= request\_id）、object=chat.completion、created、model（逻辑名）、choices\[0\].message.{role, content, parsed?}（结构化时 content 为通过校验的原文，parsed 为解析后的对象）、choices\[0\].finish\_reason（stop / length / content\_filter）、usage.{prompt\_tokens, completion\_tokens, total\_tokens, prompt\_tokens\_details.cached\_tokens, completion\_tokens\_details.reasoning\_tokens}、gateway.{request\_id, upstream\_model, protocol, retry\_count, repair\_count, latency\_ms, prompt:{name, version}}。 响应头：X-Request-ID。

流式响应（SSE）：Content-Type: text/event-stream，Cache-Control: no-cache，X-Accel-Buffering: no。依次为：首块 delta.role=assistant；若干块 delta.content；末块带 finish\_reason、usage、gateway；最后 data: \[DONE\]。空闲超过 heartbeat 间隔发 `: keep-alive` 注释行。首块已发出后出错，发 `event: error` 帧（内容为统一错误信封）后关闭连接，不再发 \[DONE\]。

### 4.2 其他端点

- GET /v1/models：逻辑模型列表及能力。GET /healthz、GET /readyz。
- 模板管理：POST /v1/prompts（创建一个新版本）；GET /v1/prompts（列表）；GET /v1/prompts/{name}/versions/{version|latest}；POST /v1/prompts/{name}/versions/{version|latest}/render（只渲染预览，不调用上游）。版本不可变，不提供更新与删除（PUT / DELETE 返回 405）。
- 可观测性（由插件挂载）：GET /admin/observability/traces（过滤 model、status、since、limit）、GET /admin/observability/traces/{request\_id}、GET /admin/observability/stats。
- 网关自身鉴权可选：配置 auth.enabled=true 时，所有端点要求 Authorization: Bearer <网关密钥>，失败 401 auth\_failed；默认关闭。

### 4.3 配置示例（config/gateway.yaml，模型名与地址均已按 DeepSeek 官方文档核对，仍以配置为准）

```
server: {host: 0.0.0.0, port: 8000}
auth: {enabled: false, keys_env: GATEWAY_API_KEYS}
retry: {max_retries: 3, base_delay_s: 0.5, factor: 2.0, max_delay_s: 8.0, jitter: full}
stream: {heartbeat_interval_s: 15}
structured: {max_repairs: 2}
defaults:
  timeout: {connect_s: 5, read_s: 60, total_s: 120}
  rate_limit: {rate_per_s: 5, burst: 10}
observability: {enabled: true, db_path: data/observability.db, queue_size: 10000}
prompts: {db_path: data/prompts.db, seed_dir: config/prompts}
models:
  deepseek-v4-pro:
    protocol: openai_responses
    base_url: https://api.deepseek.com
    endpoint_path: /responses
    api_key_env: DEEPSEEK_API_KEY
    upstream_model: deepseek-v4-pro
    rate_limit: {rate_per_s: 5, burst: 10}
    capabilities: {stream: true, native_json_object: true, native_json_schema: true}
    thinking: disabled
  deepseek-v4-flash:
    protocol: anthropic_messages
    base_url: https://api.deepseek.com/anthropic
    endpoint_path: /v1/messages
    api_key_env: DEEPSEEK_API_KEY
    upstream_model: deepseek-v4-flash
    default_max_tokens: 4096
    rate_limit: {rate_per_s: 5, burst: 10}
    capabilities: {stream: true, native_json_object: false, native_json_schema: false}
    thinking: disabled
```

验证脚本使用另一份配置，把两个 base\_url 指向本地 mock，并把限流、退避设成便于测试的值。

### 4.4 统一错误信封与错误码

所有错误响应体统一为 error 对象，字段：code、message、type（invalid\_request\_error / rate\_limit\_error / upstream\_error / structured\_output\_error / gateway\_error）、stage、retryable、request\_id、details（可选）。

| code | HTTP | stage | 网关内部是否重试 | 触发条件 |
| --- | --- | --- | --- | --- |
| invalid\_request | 422 | request | 否 | 缺字段、未知字段、取值越界 |
| unsupported\_parameter\_combination | 400 | request | 否 | 如 stream 与 response\_format 同用 |
| invalid\_response\_format | 422 | request | 否 | 请求里的 JSON Schema 本身非法 |
| unsupported\_capability | 400 | routing | 否 | 模型不具备所请求的能力 |
| model\_not\_found | 404 | routing | 否 | model 不在配置白名单 |
| auth\_failed | 401 | admission | 否 | 网关鉴权开启且失败 |
| rate\_limit\_exceeded | 429 | admission | 否 | 网关按模型限流超限，带 Retry-After |
| template\_not\_found | 404 | template | 否 | 模板或版本不存在 |
| template\_variable\_missing | 422 | template | 否 | 缺必填变量 |
| template\_render\_error | 422 | template | 否 | 传入未声明变量、变量类型非法或过长 |
| template\_conflict | 409 | template | 否 | 同 name+version 重复创建 |
| upstream\_connection\_error | 502 | connect | 是 | DNS、连接失败、连接被重置 |
| upstream\_timeout | 504 | connect | 是 | 连接或读取超时 |
| upstream\_rate\_limited | 503 | upstream\_http | 是 | 上游返回 429 |
| upstream\_server\_error | 502 | upstream\_http | 是 | 上游 500 / 502 / 504 |
| upstream\_overloaded | 503 | upstream\_http | 是 | 上游 503 / 529 |
| upstream\_auth\_error | 502 | upstream\_http | 否 | 上游 401 / 403（网关凭证问题，记高优先级日志） |
| upstream\_insufficient\_balance | 502 | upstream\_http | 否 | 上游 402 |
| upstream\_bad\_request | 502 | upstream\_http | 否 | 上游 400 / 404 / 422 |
| upstream\_protocol\_error | 502 | protocol | 否 | 200 但响应无法解析或缺关键字段 |
| upstream\_empty\_response | 502 | protocol | 否 | 200 但没有任何文本 |
| stream\_interrupted | SSE error 帧 | stream | 否 | 流中途断开或没有终止事件 |
| structured\_output\_invalid | 502 | output\_validation | 否 | 修复次数用尽仍不合法 |
| structured\_output\_truncated | 502 | output\_validation | 否 | 因 max\_tokens 截断，修复无意义 |
| internal\_error | 500 | internal | 否 | 未预期异常 |

约定：HTTP 429 只代表网关自己的限流；上游 429 一律映射为 upstream\_rate\_limited(503)，客户端据此能区分是谁限的流。错误信息不得包含 API Key、鉴权头、堆栈、上游原始响应全文（只保留脱敏后不超过 300 字符的摘要）。内部日志记录异常类型、request\_id、已发出事件数、用量。

## 5. 功能点：设计要点与验证要点

约定：每个验证编号在 verify\_all.py 里都要有同编号的检查，报告中给出「关键数字」作为证据（如上游调用次数、假 sleep 记录的延迟序列、trace 字段值）。除标注 live 的项外，全部用本地 mock 上游完成，不依赖真实 Key。

### F1 统一抽象层与路由（宏观要求 2）

**【设计要点】**

- ModelRegistry 从 gateway.yaml 载入 ModelSpec（name、protocol、base\_url、endpoint\_path、api\_key\_env、upstream\_model、timeout、rate\_limit、capabilities、thinking、default\_max\_tokens）。启动时校验配置，环境变量缺失时快速失败并指出哪个变量。
- Router.resolve(model) 返回 RouteDecision{spec, reason=static}；未知模型 404 model\_not\_found，details 列出可用模型名。
- ProviderRegistry 把 protocol 字符串映射到适配器类。新增协议 = 新增一个适配器文件 + 注册一行，api 与 services 零改动。
- 适配器基类 Provider：capabilities；complete(UnifiedRequest) → UnifiedResponse；stream(UnifiedRequest) → AsyncIterator\[StreamEvent\]。适配器在 provider 内部维护所有 httpx 客户端（复用连接池，并在关机时关闭）。
- 内部类型：UnifiedRequest（messages、temperature、top\_p、max\_tokens、stop、response\_format）、UnifiedResponse（text、finish\_reason、usage、upstream\_model）、StreamEvent（Start / TextDelta / UsageEvent / Done / Error）、Usage（见 F2）。对外 schema 与内部类型分开定义，互不复用。
- finish\_reason 统一为 stop / length / content\_filter / error。
- 能力不满足（如模型配置了 stream: false）在 service 层直接 400 unsupported\_capability，不发起上游请求。
- 非透明代理：入口 extra=forbid；上游请求体由适配器从内部类型重新构造；客户端的任何请求头都不转发。这样参数治理、校验、统一错误语义和成本统计才不会丢。

**【验证要点】**

- V-ROUTE-01 请求 deepseek-v4-pro 时只有 mock-responses 收到请求、mock-anthropic 为 0 次；请求 deepseek-v4-flash 时相反。证据：两个 mock 的调用计数。
- V-ROUTE-02 未知 model → 404 model\_not\_found，上游零调用，details 含可用模型列表。
- V-ROUTE-03 用临时配置新增第三个逻辑模型（复用现有协议），不改任何代码即可路由成功。
- V-ROUTE-04 静态检查：src/ 下不出现写死的模型名、域名、Key 样式字符串（白名单：配置文件、测试、README）。
- V-ROUTE-05 两个模型对同一请求返回的响应字段集合完全一致，客户端无法从结构上分辨上游协议。
- V-ROUTE-06 非透明：请求带未知字段 → 422；客户端发来的 Authorization 与自定义头不出现在 mock 记录的上游请求里。
- V-ROUTE-07 模型能力不满足 → 400 unsupported\_capability，上游零调用。

### F2 协议适配器与 Usage 归一化（宏观要求 1、2）

**【设计要点 · OpenAI Responses 适配器（deepseek-v4-pro）】**

- 请求：POST {base\_url}{endpoint\_path}（即 https://api.deepseek.com/responses），Header Authorization: Bearer \<key>。
- Body：model=upstream\_model；instructions=所有 system 消息按换行合并；input=其余消息的 role/content 列表；max\_output\_tokens；temperature；top\_p；stream；结构化时带 text.format（json\_object 或 json\_schema，含 name、schema、strict）；thinking=disabled 时带 reasoning.effort=none。不要发送官方标注为不支持的字段（store、previous\_response\_id、stream\_options、metadata 等）。
- 响应：取 output 中 type=message 项的 content里 output\_text 的 text 依次拼接；status=completed → stop；status=incomplete 且 incomplete\_details.reason=max\_output\_tokens → length；status=failed 或带 error → 按 F6 映射为错误。
- 流式（SSE，无 \[DONE\]）：response.output\_text.delta → TextDelta；忽略 response.reasoning\_text.delta 等其它事件；终止事件为 response.completed / response.incomplete / response.failed 三选一，其 response.usage 才是最终用量（之前事件里 usage 为 null）。

**【设计要点 · Anthropic Messages 适配器（deepseek-v4-flash）】**

- 请求：POST {base\_url}{endpoint\_path}（即 https://api.deepseek.com/anthropic/v1/messages），Header x-api-key、anthropic-version: 2023-06-01。
- Body：model；max\_tokens（必带，缺省用 default\_max\_tokens）；system 为顶层字段（system 消息合并）；messages 仅含 user / assistant，相邻同角色合并且以 user 开头；temperature、top\_p、stop\_sequences、stream；thinking=disabled 时带 thinking.type=disabled。不发送 Anthropic 不认识的 response\_format。
- 响应：content 中 type=text 的块依次拼接；stop\_reason：end\_turn / stop\_sequence → stop，max\_tokens → length，refusal → content\_filter。
- 流式：message\_start（含初始 usage）→ content\_block\_start → content\_block\_delta（仅 delta.type=text\_delta 产出 TextDelta，thinking\_delta 忽略）→ content\_block\_stop → message\_delta（含 stop\_reason 与最终 usage）→ message\_stop；ping 忽略；event: error 或无事件名的 data 帧【内容为 error 对象】→ Error；EOF 前没有 message\_stop → stream\_interrupted。

**【设计要点 · Usage 归一化（各适配器自己完成）】** 统一结构 Usage{input\_tokens, output\_tokens, cache\_read\_tokens, cache\_creation\_tokens, reasoning\_tokens, total\_tokens}，其中 input\_tokens 包含缓存命中部分。

- Responses：input=usage.input\_tokens；cache\_read=input\_tokens\_details.cached\_tokens；output=output\_tokens；reasoning=output\_tokens\_details.reasoning\_tokens；total=total\_tokens，缺失则 input+output。
- Anthropic：input = input\_tokens + cache\_read\_input\_tokens + cache\_creation\_input\_tokens（DeepSeek 的 input\_tokens 不含缓存命中）；cache\_read / cache\_creation 取对应字段；output=output\_tokens；reasoning 上游不单独给则为 null。流式用量以最终 message\_delta.usage 为准，缺的字段才用 message\_start 补，禁止两者相加。
- 上游没给 usage → 全部字段 null 并标记 usage\_missing=true，不得估算或编造；同一请求内多次上游调用（结构化修复）的 usage 逐项累加。
- 鉴权差异只存在于适配器内部；API Key 有且只有适配器读环境变量。

**【验证要点】**

- V-ADP-01 pro 请求契约：mock-responses 校验并记录：Bearer 头、body 含 instructions / input / model=upstream\_model / max\_output\_tokens，不含 messages / system / max\_tokens / store。
- V-ADP-02 flash 请求契约：x-api-key 头、system 为顶层字段、messages 中无 system 角色且首条为 user、max\_tokens 存在。
- V-ADP-03 多条 system 消息在两个适配器中都被正确合并（顺序保持）；相邻同角色 user 消息在 flash 中被合并。
- V-ADP-04 响应归一：表驱动用例覆盖 stop / length / content\_filter，两个协议的原始响应都被归一为相同的 UnifiedResponse 形态。
- V-ADP-05 Usage 归一：Responses 输入 {input 100, cached 30, output 50, reasoning 20} → input=100、cache\_read=30、output=50、reasoning=20、total=150；Anthropic 输入 {input 70, cache\_read 30, cache\_creation 0, output 50} → input=100、cache\_read=30、output=50、total=150。
- V-ADP-06 上游缺 usage → 各字段 null 且 usage\_missing=true。
- V-ADP-07 任何响应体、响应头、日志、trace 中都不出现 API Key：用唯一哨兵 Key 跑完整套验证后全量检索。
- V-ADP-08（live，可选）真实上游冡烟：两个模型各成功调用一次非流式与一次流式，不断言具体文本，只断言结构与 usage 非空；没有 Key 时标记 SKIP。

### F3 流式输出（宏观要求 3.1；课程第 2 讲）

**【设计要点】**

- 三层概念不要混：HTTP 分块传输、SSE 协议、模型 token 三者不一一对应——一次读取可能只有半条 SSE 事件也可能有多条，一个 token 可能是多字节或空串。不得假设对齐。
- sse\_parser：增量字节输入、增量 UTF-8 解码、按空行切事件，支持 event / data（多行拼接）/ id / retry、注释行（冒号开头）、CRLF 与 LF，不依赖 chunk 边界。
- 内部事件先行：适配器把供应商事件转成 StreamEvent，网关在 api/sse.py 一处统一编码为对外 SSE；供应商 chunk 不直接下发。
- **提交点**：第一个文本 delta 写给客户端的那一刻。在创建 StreamingResponse 之前，service 先 await 上游第一个有意义事件（首个文本 delta、终止或错误），这一段处在重试保护内：失败则返回普通 JSON 错误响应（带正确 HTTP 状态码）并按 F8 重试。提交之后失败只能发 event: error，不重试、不换模型——已经产生的内容重试会造成重复或前后矛盾（课程要点：已产生业务事实后不能盲目重试，即使 temperature=0 也不保证两次一致）。
- 终态恰好三选一：completed / failed / cancelled，每个流恰好落一次。
- **取消**：客户端断开只是取消信号，必须穿透到上游——关闭上游 httpx 响应与连接并释放资源，trace 记 cancelled；不得让上游继续生成。用 try/finally 保证清理。
- 心跳：上游沉默超过 heartbeat\_interval\_s，向客户端发 `: keep-alive`。
- TTFT：从网关收到请求起，到收到上游第一个「非空」文本 delta 为止；思考类 delta 不算；非流式请求 ttft 为 null。
- 流式调用的 usage 由适配器从上游终止事件里取，网关不自己统计。

**【验证要点】**（流式检查用真实 socket 的 uvicorn，不用进程内伪造）

- V-STREAM-01 不是缓冲后一次性输出：mock 每 100ms 发一块共 5 块，客户端记录每块到达时间，首块到达时间早于最后一块由上游发出的时间，块间隔与上游近似。
- V-STREAM-02 格式：首块 role、中间 content delta、末块 finish\_reason + usage、最后 \[DONE\]；把所有 delta 拼起来等于同输入的非流式 content。
- V-STREAM-03 deepseek-v4-pro 与 deepseek-v4-flash 都通过 01–02。
- V-STREAM-04 供应商事件不泄漏：客户端流里不出现 response.\* 或 content\_block\_\* 等事件名。
- V-STREAM-05 解析鲁棒（单测）：同一段上游字节流按 1 字节切、按多事件粘包、中文多字节被切开、混用 CRLF、夹杂注释行与 ping，解析结果一致。
- V-STREAM-06 提交前失败可重试：mock 前两次直接 503、第三次正常 → 客户端得到完整正常流，retry\_count=2，上游调用 3 次。
- V-STREAM-07 提交前的不可重试错误（上游 401）→ 返回普通 JSON 错误（HTTP 状态码非 200），而不是 SSE；上游调用 1 次。
- V-STREAM-08 提交后上游中断：mock 发出 2 块后断开 → 客户端收到 event: error 帧并结束，上游调用恰为 1 次，trace.status=failed且记录已发出字符数 > 0。
- V-STREAM-09 客户端中途断开 → ≤ 2s 内 mock 观察到上游连接被关闭，trace.status=cancelled，网关进程无泄漏的任务（活跃连接数回落到 0）。
- V-STREAM-10 上游没有终止事件就 EOF（两个协议各一个用例）→ stream\_interrupted，不被当成成功。
- V-STREAM-11 心跳：上游沉默超过 heartbeat 间隔（验证配置里设为 0.3s）客户端收到 keep-alive 注释。
- V-STREAM-12 stream=true 与 response\_format≠text 同时给 → 400 unsupported\_parameter\_combination，上游零调用。
- V-STREAM-13 TTFT：mock 首块延迟 300ms → trace.ttft\_ms 在 \[300, 450\]、且 < total\_ms。
- V-STREAM-14 流式 usage：末块 usage 与上游终止事件一致；Anthropic 流里 message\_start 与 message\_delta 的 usage 没有被相加。

### F4 结构化输出（宏观要求 3.2；课程第 6、7 讲）

**【设计要点】**

- 请求：response\_format.type 为 json\_object（合法 JSON 对象）或 json\_schema（符合给定 Schema）。网关先用 jsonschema 校验该 Schema 自身合法，否则 422 invalid\_response\_format。
- 两条路径，但最终校验完全一致：① 模型支持原生结构化（pro）→ 适配器走 text.format；② 不支持（flash）→ service 在 system 消息中追加「只输出符合以下 JSON Schema 的合法 JSON，不要代码围栏、不要附加说明」并附上 Schema。json\_object 模式的指令里必须出现「JSON」字样。原生不等于可信：两条路径都要本地再校验。提示词只能表达期望，不能验证结果。
- 本地校验三层：① 语法（json.loads 严格解析，不做任何静默修正，包括不自动去掉 markdown 代码围栏）；② 字段与类型（jsonschema，收集全部错误的路径与类型）；json\_object 模式要求顶层是对象；③ 业务语义（提供 validators 扩展钩子，默认为空）。三层任一层失败都不得作为成功返回。
- 有限修复循环（repair loop）：校验失败且还有预算时，把原消息 + 模型上一次的非法输出 + 一条只含「错误位置与类型」的修正指令再发给模型，指令要求「只修正出错的语法和字段，其余保持不变，只输出 JSON」；不得泄露网关内部策略。max\_repairs 默认 2，修复次数与网络重试次数是两个独立预算，代码里分开计数。每次修复调用仍走同一套重试与错误分类。
- 不适合修复的情况直接失败：finish\_reason=length（截断）→ structured\_output\_truncated，不修复；网络超时与限流走重试，不让模型修。
- 预算用尽仍不合法 → 502 structured\_output\_invalid，details 只含最后一次的错误路径与类型和 repair\_count，不把非法输出当成功内容返回。
- 成功响应：content = 通过校验的模型原文，parsed = 解析后的对象，gateway.repair\_count 记录修复次数，usage 为所有调用累加。
- 与 stream 互斥（见 4.1）。

**【验证要点】**

- V-STRUCT-01 json\_object 在两个模型上都成功，parsed 为对象，content 可被 json.loads 解析。
- V-STRUCT-02 json\_schema 在两个模型上都成功，parsed 满足给定 Schema（用独立的 jsonschema 调用再验一遍）。
- V-STRUCT-03 路径差异：mock-responses 记录到 text.format；mock-anthropic 的请求体不含任何原生结构化字段、但 system 中含 Schema 说明与「JSON」字样。
- V-STRUCT-04 语法错误修复：mock 第 1 次返回缺括号的 JSON、第 2 次合法 → 200，repair\_count=1，上游调用 2 次，修复请求里含错误位置与类型、不含网关内部策略文字，usage 为两次之和。
- V-STRUCT-05 Schema 违反（缺必填字段 / 类型错）同样触发修复并成功。
- V-STRUCT-06 预算耗尽：连续 3 次都非法 → 502 structured\_output\_invalid，上游调用恰为 3 次（1 + 2 次修复），响应体不以成功形态包含非法输出。
- V-STRUCT-07 截断：上游返回 finish\_reason=length 且 JSON 不完整 → structured\_output\_truncated，上游调用 1 次（无修复、无重试）。
- V-STRUCT-08 用 markdown 代码围栏包起来的 JSON 不会被静默接受，而是触发修复。
- V-STRUCT-09 json\_object 模式下返回顶层数组 → 视为非法。
- V-STRUCT-10 请求里的 Schema 本身非法 → 422 invalid\_response\_format，上游零调用。
- V-STRUCT-11 预算独立：上游先 503 两次（重试）再返回非法 JSON、再返回合法 JSON → 成功，retry\_count=2、repair\_count=1，互不占用。

### F5 提示词模板与版本管理（宏观要求 3.3；课程第 4、8 讲）

**【设计要点】**

- 模板像函数，版本像一整段代码：由网关统一发布和管理，调用方只传 name、version、variables，不能修改模板内容。
- 数据模型：name、version（SemVer X.Y.Z，创建者指定，同 name 内唯一且不可变）、description、owner、changelog、messages（role + content 列表，content 里用 {{ 变量名 }} 占位）、variables（每个包含 name、required、default、max\_length）、created\_at。
- 引用：请求里 prompt.name + prompt.version；version 缺省或 latest = 最高 SemVer。trace 与响应里记录实际解析出的版本，这样成功率变化能归因到模板版本而不是模型。
- 渲染：自己写的单次扫描替换，不递归；不得用 eval / str.format / 不受限的 Jinja（避免模板注入）。变量值仅允许 str / int / float / bool，一律转字符串，超过 max\_length（默认 20000）拒绝。
- 校验：创建时静态检查——占位符必须都已声明、声明的变量必须被使用、role 合法；调用时——必填变量缺失 422 template\_variable\_missing，多传未声明变量或类型非法 422 template\_render\_error。所有模板错误都在请求上游之前报出。
- 指令与数据分离（注入防护第一层）：角色、规则只能写在模板里；变量值只会出现在模板指定的位置，渲染结果的角色由模板决定；调用方的额外 messages 只能是 user / assistant，不能追加 system。README 里建议模板用分隔标签包住变量。
- 存储：独立 SQLite（prompts.db），启动时从 config/prompts/\*.yaml 幂等导入种子（同 name+version 内容不一致则启动报错，不覆盖）。模板路径不走任何可观测性代码。
- 种子：code-review 1.0.0 与 1.1.0（分析 / 审查 / 检查三段式，1.1.0 增加可靠性与可观测性风险维度，两版渲染结果可区分）、summarize 1.0.0。模板写法用工程化风格：先定义岗位再交代任务、列出步骤与边界，不写「你是世界顶级专家」这类无法验证的话。

**【验证要点】**

- V-TPL-01 创建模板 → 201；同 name+version 重复创建 → 409 template\_conflict。
- V-TPL-02 按 name + version 调用：mock 记录的上游消息与独立计算的渲染结果逐字一致（两个模型各一次）。
- V-TPL-03 两个版本内容不同，分别引用得到不同渲染结果；不写 version 与 latest 都解析为最高版本，响应与 trace 里记的是解析后的具体版本。
- V-TPL-04 缺必填变量 → 422 template\_variable\_missing，上游零调用；有 default 的变量可省略并使用默认值。
- V-TPL-05 多传未声明变量、传入对象 / 列表、值超长 → 422 template\_render\_error。
- V-TPL-06 模板或版本不存在 → 404 template\_not\_found。
- V-TPL-07 变量值里含 {{ other }} 或「忽略以上指令」之类文本 → 原样作为数据出现在指定位置，不被二次渲染，也不会变成 system 消息。
- V-TPL-08 调用方不能覆盖模板：prompt 与 messages 同时给且 messages 含 system → 422。
- V-TPL-09 创建时静态检查：占位符未声明、声明未使用、role 非法、version 不是 SemVer 都被拒绝。
- V-TPL-10 持久化：重启网关后模板仍在；版本不可变（PUT / DELETE 返回 405）。
- V-TPL-11 trace 记录 prompt\_name、prompt\_version、渲染结果指纹。
- V-TPL-12 render 预览端点不调用上游（mock 计数不变）。
- V-TPL-13 模板可与 stream、response\_format 分别组合使用。
- V-TPL-14 种子导入幂等：连续启动两次不重复、不报错；种子内容被改后仍用同版本号启动 → 报错退出。

### F6 可观测性插件（宏观要求 4 + 要求第 3 条；课程第 8 讲）

**【设计要点】**

- 事件总线：core/events.py 定义 EventBus（同步 emit，订阅者回调在总线内 try/except 隔离）和事件类型：RequestStarted、AttemptFinished、FirstTokenReceived、RequestFinished（唯一的终态事件，携带完整汇总）。services 与 providers 只发事件，不 import observability。
- 插件装配：observability/plugin.py 提供 install(app, bus, settings)，创建 Recorder 订阅总线、挂载查询路由、关机时 flush；main.py 按 observability.enabled 决定是否调用。
- 写入：Recorder 只把记录 put\_nowait 进有界队列（满则丢弃并累加 dropped 计数），由后台线程批量写 SQLite（WAL，CREATE TABLE IF NOT EXISTS），热路径不做磁盘 IO。持久化到 SQLite，不写内存。
- 每次请求恰好一条 trace（成功、失败、拒绝、取消各一条）。字段：request\_id、ts\_start、logical\_model、upstream\_model、protocol、stream、prompt\_name、prompt\_version、prompt\_fingerprint（渲染后消息的 sha256）、prompt\_chars、output\_chars、status（completed / failed / rejected / cancelled）、finish\_reason、error\_code、error\_stage、retry\_count、repair\_count、attempts（JSON：序号、耗时、错误码、退避时长）、input\_tokens、output\_tokens、cache\_read\_tokens、cache\_creation\_tokens、reasoning\_tokens、total\_tokens、usage\_missing、latency\_total\_ms、ttft\_ms、generation\_ms（= total − ttft，非流式为 null）、pre\_upstream\_ms（校验 / 模板 / 限流耗时）、retry\_wait\_ms（退避总和）、emitted\_chars、cost\_estimate（可选，配置了价格表才有，否则 null）。
- 拆分延迟的理由（课程）：总延迟高不等于模型慢，可能是排队、模板加载或退避等待，所以分开记。记录「请求的逻辑模型」与「实际上游模型」两个字段，为日后降级留口子。
- Token 分类口径见 F2。
- 查询：traces（过滤、分页）、traces/{id}、stats（按模型聚合：次数、成功率、各类 token 总和、total 与 ttft 的 avg / p50 / p95、重试总次数、各 error\_code 计数）。
- 隐私：默认不存完整提示词与输出，只存指纹与字符数；内容采样如要做必须是显式配置、脱敏、带保留期、默认关闭（本期可不实现）。
- 解耦：observability 之外的代码零 import；关闭时网关行为不变；提供 import 方向的静态检查脚本。

**【验证要点】**

- V-OBS-01 每次调用恰一条 trace（成功 / 失败 / 拒绝 / 取消各验一次），request\_id 与响应头 X-Request-ID 一致。
- V-OBS-02 Token 分类：mock 返回已知 usage → trace 中 input / output / cache\_read / cache\_creation / reasoning / total 与期望值逐项相等（两个协议各一组）。
- V-OBS-03 延迟：非流式时 mock 人为延迟 400ms → total\_ms ≥ 400 且 ttft\_ms 为 null；流式时首块延迟 300ms → 300 ≤ ttft\_ms < total\_ms，generation\_ms = total − ttft。
- V-OBS-04 重试被记录：失败两次后成功 → retry\_count=2，attempts 长度 3，retry\_wait\_ms 等于假 sleep 记录的退避之和。
- V-OBS-05 失败与拒绝也有 trace：error\_code / error\_stage 与响应一致；限流拒绝的 status=rejected。
- V-OBS-06 trace 记录逻辑模型、实际上游模型、协议、模板名与版本。
- V-OBS-07 查询：traces 支持按 model、status 过滤；traces/{id} 可取单条；stats 中各类 token 总和等于逐条 trace 求和，成功率与实际一致。
- V-OBS-08 隐私：trace 与日志中检索不到完整提示词、完整输出、哨兵 Key，只有指纹与字符数。
- V-OBS-09 解耦静态检查（ast 扫描）：api / core / services / providers 下任何模块都没有 import observability；只有 main.py 同时引用两者。
- V-OBS-10 关闭插件（observability.enabled=false）：全部非可观测性验证项仍然通过，/admin/observability/\* 返回 404。
- V-OBS-11 故障隔离：注入「存储不可写 / 订阅者抛异常 / 队列已满」，请求仍成功且额外延迟 < 50ms，dropped 计数可见。
- V-OBS-12 持久化：重启网关后历史 trace 仍可查。

### F7 错误体系与错误分类（宏观要求 5 前半；课程第 1、8 讲）

**【设计要点】**

- 错误按发生阶段分类再决定处置：请求构造 → 准入（鉴权、限流）→ 模板 → 路由 → 网络连接 → 上游 HTTP → 协议解析 → 业务校验 → 流式。认证失败立即终止并检查凭证；参数与协议不合法不重试，让调用方修正；网络与服务端临时性错误才做有界重试；截断、拒答、格式错误不能靠重试解决。
- GatewayError(code, http\_status, stage, retryable, message, details) 是唯一错误类型。providers/http\_errors.py 是唯一把 httpx 异常、上游状态码、上游错误体映射成 GatewayError 的地方，映射表驱动（见 4.4）。上游错误体（Anthropic 风格的 type=error / error.message、OpenAI 风格的 error.message / error.code）解析失败时退回到纯状态码映射。
- 全局 exception handler 输出统一信封；未预期异常 → 500 internal\_error，同样带 request\_id，并产生 trace。
- 脱敏：统一 sanitize() 去掉 Key、Bearer 令牌、长随机串；上游错误摘要 ≤ 300 字符；不暴露供应商堆栈（可能泄露 Key）。内部日志只记异常类型、request\_id、已发出事件数、用量。
- 四层判定（网络 → HTTP → 协议 → 业务）都过才算成功：200 但不可解析 → upstream\_protocol\_error；200 但无文本 → upstream\_empty\_response。
- 不做自动降级；失败原因要记到 trace，区分「模型问题」与「系统问题」。

**【验证要点】**

- V-ERR-01 表驱动：对上游 400、401、402、403、404、422、429、500、502、503、504、529、超时、连接拒绝、非 JSON 体、缺关键字段、空文本，两个协议各跑一遍，断言网关 code / HTTP / stage / retryable 与 4.4 表一致，且上游调用次数符合可重试性（可重试 4 次，不可重试 1 次）。
- V-ERR-02 所有错误响应体符合统一信封且带 request\_id，响应头 X-Request-ID 与之一致。
- V-ERR-03 不泄露：错误体与日志不含哨兵 Key、鉴权头、堆栈、上游响应全文（上游故意返回含哨兵字符串的长错误体来验证截断与脱敏）。
- V-ERR-04 请求级校验错误（缺 model、空 messages、temperature 越界、未知字段）→ 422 / 400，上游零调用。
- V-ERR-05 适配器内部抛出未预期异常 → 500 internal\_error，信封一致，并有 trace。
- V-ERR-06 上游 HTTP 200 但 body 不是 JSON 或缺 output 字段 → upstream\_protocol\_error（不是成功）；200 但文本为空 → upstream\_empty\_response。
- V-ERR-07 上游 429 映射为 upstream\_rate\_limited(503)，不会出现 rate\_limit\_exceeded；网关自己限流才产生 rate\_limit\_exceeded(429)。

### F8 指数退避重试（宏观要求 5 中段；课程第 1、2、8 讲）

**【设计要点】**

- 独立模块 core/retry.py：RetryPolicy(max\_retries, base\_delay\_s, factor, max\_delay\_s, jitter) + Retrier；sleep、rng、单调时钟都通过构造参数注入，便于用假时钟验证。
- max\_retries=3：首次 + 最多 3 次重试 = 最多 4 次上游调用（这是对「最多 3 次」的解读，见 §9，改配置即可）。
- 延迟公式：d\_n = min(max\_delay\_s, base\_delay\_s × factor^(n−1))，n 是第几次重试。jitter：none 取 d\_n；full 取 uniform(0, d\_n)；equal 取 d\_n/2 + uniform(0, d\_n/2)。默认 full，验证配置用 none 以得到确定序列。
- 只重试 retryable=true 的 GatewayError（见 4.4）。上游给 Retry-After 时 delay = max(d\_n, retry\_after)；若 retry\_after > max\_delay\_s，不重试、立即返回，不让请求挂起。
- 总预算：timeout.total\_s 约束整个请求（含重试与修复），剩余时间不足以等完下一次退避时不再重试。
- 每次尝试都产生 attempt 记录（序号、耗时、错误码、退避时长）并通过事件总线发出；重试计数以网关自己的为准。
- 作用范围：非流式整次调用；流式仅限提交点之前；结构化修复调用各自独立套用。限流令牌在请求入口消耗一次，重试与修复不再消耗。

**【验证要点】**

- V-RETRY-01 上游持续 503 → 上游调用恰 4 次，最终 503 upstream\_overloaded，retry\_count=3。
- V-RETRY-02 指数退避：jitter=none、base=0.5、factor=2 时，假 sleep 记录到的序列为 \[0.5, 1.0, 2.0\]；jitter=full 时每次延迟位于 \[0, 对应 d\_n\]。
- V-RETRY-03 max\_delay 封顶：base=3、factor=4、max=8 → \[3, 8, 8\]。
- V-RETRY-04 不可重试错误（400 / 401 / 402 / 403 / 422、协议错误、结构化截断）→ 上游调用恰 1 次。
- V-RETRY-05 503、503、再 200 → 成功，retry\_count=2，上游调用 3 次。
- V-RETRY-06 429 带 Retry-After: 2 → 假 sleep 的值 ≥ 2；Retry-After 超过 max\_delay\_s → 不重试立即返回（上游 1 次）。
- V-RETRY-07 超时与连接错误同样可重试，次数同 01。
- V-RETRY-08 总预算：total\_s 耗尽后不再重试，上游调用次数 < 4。
- V-RETRY-09 无隐式重试：mock 统计的调用次数与 trace.attempts 长度逐次相等。
- V-RETRY-10 流式重试边界见 V-STREAM-06 / 07 / 08；重试与修复预算独立见 V-STRUCT-11。
- V-RETRY-11 整套退避相关检查的真实总耗时 < 5s（证明没有真实 sleep）。

### F9 按模型独立限流（宏观要求 5 后半；课程第 9 讲）

**【设计要点】**

- 令牌桶：每个逻辑模型一个桶，参数 rate\_per\_s、burst，初始满桶；惰性补充（按时间差计算，不用后台线程）。
- 准入时非阻塞 try\_acquire(1)；失败 → 429 rate\_limit\_exceeded，Retry-After = ceil(缺口 / rate)（至少 1），details 含 model、rate\_per\_s、burst、retry\_after\_s。成功后才继续模板、路由、上游。
- try\_acquire 是不含 await 的同步方法，在单个事件循环内天然原子；限流范围是进程内，多 worker 部署的局限写进 README。
- 模型之间相互独立：A 模型耗尽不影响 B。流式与非流式同样受限，且在建立上游连接之前判定。
- 被拒请求：不重试、不调上游、仍产生 trace（status=rejected）。时钟可注入（Clock 接口）。未单独配置的模型使用 defaults.rate\_limit。

**【验证要点】**

- V-RL-01 burst=3：瞬时 5 个并发请求，恰 3 个成功、2 个 429，上游收到 3 次。
- V-RL-02 429 响应体 code=rate\_limit\_exceeded，带 Retry-After 头（整数秒 ≥ 1）与 details。
- V-RL-03 按模型独立：pro 桶耗尽时 flash 请求仍成功，反之亦然。
- V-RL-04 恢复：假时钟推进 1/rate 秒后放行一个。
- V-RL-05 流式请求同样受限，被拒请求不建立上游连接。
- V-RL-06 并发安全：200 并发打向 burst=50 的桶，通过数恰为 50。
- V-RL-07 被拒请求有 trace（status=rejected），不进入重试。
- V-RL-08 一次客户端请求只消耗一个令牌：触发 3 次重试的请求不会多扣令牌。

## 6. 验证体系（课程第 8 讲：分层验证，不要只测最终结果）

### 6.1 分轮与分层

验证按 7 轮建立，每轮内部再分层：适配器层（请求契约、响应归一、finish\_reason 映射、usage、状态码映射）、路由层（能力匹配）、重试器（注入 sleep / rng / 时钟）、流式层、结构化层、可观测层。

- R1 内部协议 + 模拟适配器：F1、F7 的信封与入口校验
- R2 真实适配器结构：F2（请求契约、配置读取、超时映射、供应商标识进入 trace）
- R3 流式与取消：F3（内部事件 → SSE、客户端断开统计）
- R4 结构化输出：F4
- R5 路由、重试、限流：F1、F7、F8、F9
- R6 提示词模板：F5
- R7 资源管理与可观测闭环：F6

### 6.2 Mock 上游（tests/mock\_upstream，必须自己实现）

- 一个 FastAPI 应用，同时模拟 POST /responses（OpenAI Responses，含非流式与 SSE，事件序列与真实协议一致，无 \[DONE\]）和 POST /anthropic/v1/messages（Anthropic Messages，含非流式与完整的 message\_start … message\_stop 事件序列）。两个协议分别计数。
- 严格校验入站请求符合协议（鉴权头、必填字段、不该出现的字段），不符合返回 400 并记录原因，这就是 V-ADP-01 / 02 的证据来源。
- 控制接口（不走网关，网关是非透明的，不能把场景信息夹在客户端请求里）：POST /\_\_control 设置场景；GET /\_\_stats 返回调用次数、最近请求头与请求体、当前活跃连接数、是否观察到客户端（网关）断开；POST /\_\_reset。
- 场景支持「按调用序号的序列」，例如 \[503, 503, 200\]。可用场景：正常、指定状态码（可带 Retry-After）、连接掉线、超时（不响应）、非 JSON 体、空文本、截断（length / max\_tokens）、自定义文本与 usage、首块延迟、块间隔、流中途关闭、流无终止事件、按序号返回不同文本（用于修复循环）。

### 6.3 scripts/verify\_all.py

- create\_app(settings, clock=..., sleep=..., rng=...) 工厂允许注入时钟、sleep、随机源；verify\_all 在同一进程内用 uvicorn.Server 以真实 socket 启动 mock 与网关（随机端口，使用 config/gateway.verify.yaml），这样既能注入假时钟验证退避、又能真实验证断开与流式行为。
- 输出：控制台表格 + verification\_report.md + verification\_report.json，每个 V-编号一行：编号 | 检查内容 | PASS / FAIL / SKIP / DEFERRED | 证据（关键数字）。SKIP 只允许用于 live 项；依赖 trace 的检查在 M7 前可标 DEFERRED，但最终报告中不允许出现 DEFERRED。
- 报告末尾给出「六大能力验收汇总」：两个模型调用（V-ROUTE-01、V-ADP-01 / 02 / 04）、流式（V-STREAM-*）、结构化（V-STRUCT-*）、模板引用（V-TPL-*）、可观测数据（V-OBS-*）、重试与限流（V-RETRY-*、V-RL-*），对应编号全部 PASS 才标 OK。
- 完整性自检：启动时解析 docs/blueprint/04-verification-plan.md 里的编号集合，与脚本注册的检查集合比对，缺项或多项都算 FAIL。
- 退出码：任一 FAIL → 1。支持 --only <前缀>、--upto <里程碑>、--live。总耗时目标 < 90s。
- 哨兵检索：用唯一的哨兵 API Key 跑完整套验证，最后在所有响应、响应头、捕获的日志、trace 数据库、模板库中检索。
- 另有 pytest 单测：sse\_parser、retry、ratelimit、usage 归一、模板渲染与静态检查、结构化校验器、http\_errors 映射。
- live 模式：只在环境变量里有真实 Key 时运行 V-ADP-08，否则 SKIP。

## 7. 交付物

1. 完整源码（按 §3 目录）。
2. README.md：项目简介与架构图；安装；配置说明（yaml 字段、环境变量）；启动指南（连真实上游；连本地 mock）；curl 示例——至少包含：健康检查、模型列表、非流式 pro、非流式 flash、流式 pro（curl -N）、流式 flash、json\_object、json\_schema、创建模板、按版本引用模板、render 预览、触发 429（循环快速请求）、查询 traces、查询 stats、运行验证脚本；错误码表；已知局限（进程内限流、非目标清单）；扩展指南（新增协议、新增模型）；可观测性插件的启用 / 关闭 / 卸载方式。
3. 验证脚本 scripts/verify\_all.py，覆盖 §5 全部编号，附最新一次 verification\_report.md。
4. scripts/demo\_curl.sh：把 README 的 curl 示例在本地 mock 配置下全部实跑一遍并输出结果（保证 README 不是纸面文档）。
5. docs/blueprint/ 全套蓝图与 docs/review-log.md。
6. config/gateway.yaml、config/gateway.verify.yaml、config/prompts/\*.yaml、.env.example（只含变量名，不含真实 Key）。
7. tests/ 下的单元测试与 mock 上游。

## 8. 实施里程碑与门禁

每个里程碑的门禁都一样：pytest 全绿 + `verify_all.py --upto <该里程碑>` 全绿 + review-log.md 追加一条 + 这一里程碑对已有功能的影响已按 §1 第 4 条审视。

- M0 蓝图：docs/blueprint/ 全套，覆盖 §5 的每个验证编号，列出集中提问（如无则写「无阻塞性问题」）。
- M1 骨架：配置加载、core 类型 / 错误 / 事件总线 / 时钟、对外 schemas、FastAPI 装配、mock 上游、用假适配器跑通非流式链路。验证：V-ERR-02 / 04 / 05、V-ROUTE-02 / 06 / 07 的框架部分。
- M2 两个真实适配器（非流式）+ usage 归一 + 路由 + http\_errors 映射。验证：V-ROUTE-*、V-ADP-*、V-ERR-01 / 03 / 06。
- M3 重试 + 限流。验证：V-RETRY-*（除流式相关项）、V-RL-*（除 trace 相关项）、V-ERR-07。
- M4 流式（含 SSE 解析器、提交点、取消、心跳）。验证：V-STREAM-\*（除 trace 相关项）。
- M5 结构化输出（含修复循环）。验证：V-STRUCT-\*。
- M6 提示词模板（含种子与管理端点）。验证：V-TPL-\*（除 trace 相关项）。
- M7 可观测性插件。验证：V-OBS-\*，并把前面标 DEFERRED 的 trace 相关检查全部转为 PASS。
- M8 收尾：verify\_all 汇总、README、demo\_curl.sh、全量回归、解耦检查、无硬编码检查、最终报告。

## 9. 默认假设与决策（写进 05-decisions.md；可被你更合理的方案覆盖，覆盖时要记录原因）

- A1 对外统一入口采用 OpenAI Chat Completions 风格（客户端最熟悉），对两个上游协议各做翻译。
- A2 「最多 3 次」理解为最多 3 次重试（最多 4 次上游调用），可配置。
- A3 stream 与 response\_format 互斥（课程明确要求在入口报错）。
- A4 不做 fallback / 动态路由 / 工具调用 / 多模态 / 会话存储（见 §2）。
- A5 存储用 SQLite；trace 与模板分库，插件与业务互不共用连接。
- A6 trace 不存提示词与输出正文，只存指纹和字符数。
- A7 网关鉴权默认关闭，可配置开启。
- A8 TTFT = 从网关收到请求到收到上游首个非空文本 delta；非流式为 null。
- A9 限流：每个逻辑模型一个令牌桶，按请求计数，不区分调用方，进程内。
- A10 thinking 默认关闭且推理内容不对外下发（保证 JSON 输出干净、TTFT 可比）。
- A11 端点与模型名取自 DeepSeek 官方文档（2026-11 核对）：Responses = POST https://api.deepseek.com/responses（Bearer）；Anthropic = POST https://api.deepseek.com/anthropic/v1/messages（x-api-key）；模型名 deepseek-v4-pro / deepseek-v4-flash。实现前用一条 curl 实测，如与现实不符只改配置、不改代码。
- A12 flash 没有原生结构化字段 → 用「提示词注入 Schema + 本地校验 + 有限修复」保证结果；pro 的原生结构化同样要再校验。
- A13 上游缺 usage 时不估算。

## 10. 完成定义与最终汇报

**完成定义**（全部满足才算完成）：

- `python scripts/verify_all.py` 退出码 0，报告中无 FAIL、无 DEFERRED（仅 live 项可 SKIP），且 §5 的每个编号都出现在报告里并带证据。
- 「六大能力验收汇总」全部 OK；两个模型的调用均正常。
- demo\_curl.sh 实跑通过，README 中的 curl 与它一致。
- 解耦检查（V-OBS-09 / 10）与无硬编码检查（V-ROUTE-04）通过。
- review-log.md 每个里程碑至少一条；实现与蓝图一致（偏离处已回写蓝图）。

**最终汇报格式**（如实、简洁，有 SKIP / FAIL 必须列出，不要美化）：① 做了什么（一段话）；② 验证报告摘要（通过数 / 总数，六大能力汇总表）；③ 偏离默认假设或蓝图的地方及原因；④ 已知局限；⑤ 如何启动，以及如何用真实 Key 跑 live 冡烟。

现在开始：先完成 M0（蓝图），再按里程碑逐步推进。
