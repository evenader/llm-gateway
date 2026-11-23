# LLM Gateway

统一的大模型网关：对外只暴露一个 OpenAI Chat Completions 风格入口，对内通过适配器调用两种协议的上游。实现并自带验证：流式输出、结构化输出（含有限修复）、提示词模板与版本、可观测性、指数退避重试、按模型独立限流。

- 逻辑模型与协议均由 `config/gateway.yaml` 配置，代码里没有模型名、域名、Key。
- 验证：`python scripts/verify_all.py`（默认 92 项，含真实上游 live 冒烟；`--no-live` 时只跑 91 项 mock）。
- 设计文档：`docs/blueprint/`；里程碑记录：`docs/review-log.md`。

## 1. 架构

```
客户端
  │  POST /v1/chat/completions（统一 schema，extra=forbid）
  ▼
api（请求校验 / SSE 编码 / 错误信封）
  │
  ▼
services（ChatService：路由 → 限流 → 模板 → 能力检查 → 上游调用/重试/修复）
  │                 │
  │                 ├─ services/router      逻辑模型 → ModelSpec
  │                 ├─ services/prompts     模板存储/渲染/版本
  │                 └─ services/structured JSON Schema 校验与修复循环
  ▼
providers（Provider 抽象 + 两个适配器 + http_errors 映射）
  ├─ openai_responses  → POST {base_url}/responses
  └─ anthropic_messages → POST {base_url}/v1/messages

core：类型 / 错误 / 事件总线 / 重试 / 令牌桶 / 时钟
observability（插件，仅 main.py 装配）：订阅事件 → 有界队列 → SQLite → /admin/observability/*
```

依赖方向：`api → services → providers / core`；业务层不 import observability；供应商原始事件/对象不出 provider 层。

## 2. 安装

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install -e ".[dev]"
# macOS / Linux
.venv/bin/pip install -e ".[dev]"
```

环境变量（见 `.env.example`）：

| 变量 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 上游 Key，只在 providers 层读取 |
| `DEMO_UPSTREAM_KEY` | 仅本地 mock/demo 配置使用的占位 Key，与真实上游 Key 隔离，可随便填 |
| `GATEWAY_API_KEYS` | 网关自身鉴权 Key（逗号分隔），`auth.enabled=true` 时必需 |

启动前必须设置模型 `api_key_env` 指向的变量；缺失会在启动时快速失败并列出变量名。

## 3. 配置

`config/gateway.yaml` 主要字段：

| 字段 | 说明 |
| --- | --- |
| `server` | host / port（`scripts/run_gateway.py` 可用 CLI 覆盖） |
| `auth` | 网关鉴权开关与 Key 环境变量名 |
| `retry` | `max_retries`（重试次数，默认 3 → 最多 4 次调用）、`base_delay_s`、`factor`、`max_delay_s`、`jitter`（none/full/equal） |
| `stream` | `heartbeat_interval_s` |
| `structured` | `max_repairs`（默认 2） |
| `defaults` | 默认超时与限流 |
| `observability` | 开关、SQLite 路径、队列大小 |
| `prompts` | 模板库路径与种子目录 |
| `models` | 逻辑模型 → 协议、base_url、endpoint_path、api_key_env、upstream_model、timeout、rate_limit、capabilities、thinking、default_max_tokens |

`capabilities.native_json_object / native_json_schema` 只决定结构化输出走原生字段还是“提示词注入 + 本地校验”；`stream=false` / `structured_output=false` 会触发 `unsupported_capability`。

## 4. 启动

### 4.1 连接真实上游

```bash
export DEEPSEEK_API_KEY=sk-...
python scripts/run_gateway.py --config config/gateway.yaml --port 8000
```

### 4.2 连接本地 mock（无需真实 Key）

```bash
export DEMO_UPSTREAM_KEY=demo-local-key
python scripts/run_mock_upstream.py --port 9001
python scripts/run_gateway.py --config config/gateway.demo.yaml --port 8010
```

`config/gateway.demo.yaml` 把两个 base_url 指向 `127.0.0.1:9001`，并额外提供 `demo-limited`（burst=2）用于演示 429。mock 场景只认 `DEMO_UPSTREAM_KEY`，因此 `DEEPSEEK_API_KEY` 可以一直保留真实值，不需要临时改写成占位 Key。

## 5. curl 示例

以下示例与 `scripts/demo_curl.sh` 一致（本地 mock，`API=http://127.0.0.1:8010`）。

```bash
API=http://127.0.0.1:8010

# 健康检查
curl -s $API/healthz

# 模型列表
curl -s $API/v1/models

# 非流式 pro
curl -s -X POST $API/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}],"max_tokens":128}'

# 非流式 flash
curl -s -X POST $API/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}],"max_tokens":128}'

# 流式 pro（SSE）
curl -sN -X POST $API/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","stream":true,"messages":[{"role":"user","content":"hi"}],"max_tokens":128}'

# 流式 flash
curl -sN -X POST $API/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","stream":true,"messages":[{"role":"user","content":"hi"}],"max_tokens":128}'

# json_object
curl -s -X POST $API/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"give json"}],"max_tokens":128,"response_format":{"type":"json_object"}}'

# json_schema
curl -s -X POST $API/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"give json"}],"max_tokens":128,"response_format":{"type":"json_schema","json_schema":{"name":"demo","schema":{"type":"object","required":["ok"],"properties":{"ok":{"type":"boolean"}}},"strict":true}}}'

# 创建模板（版本不可变）
curl -s -X POST $API/v1/prompts -H 'Content-Type: application/json' \
  -d '{"name":"greet","version":"1.0.0","description":"demo template","owner":"demo","changelog":"initial","messages":[{"role":"system","content":"You are {{ role }}."},{"role":"user","content":"Say hello to {{ name }}"}],"variables":[{"name":"role","required":false,"default":"assistant"},{"name":"name","required":true}]}'

# 按版本引用模板
curl -s -X POST $API/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","max_tokens":128,"prompt":{"name":"code-review","version":"1.1.0","variables":{"code":"print(1)"}}}'

# render 预览（不调用上游）
curl -s -X POST $API/v1/prompts/code-review/versions/latest/render -H 'Content-Type: application/json' \
  -d '{"variables":{"code":"print(1)"}}'

# 触发 429（demo-limited burst=2）
for i in 1 2 3 4; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"demo-limited","messages":[{"role":"user","content":"hi"}],"max_tokens":64}'
done

# 查询 traces / stats
curl -s "$API/admin/observability/traces?limit=3"
curl -s "$API/admin/observability/stats"

# 运行验证脚本
python scripts/verify_all.py
```

`scripts/demo_curl.sh` 会启动 mock 与 demo 网关、实跑以上命令，并最后执行完整验证脚本；日志写入 `data/demo/`。

### 5.1 演示（showcase）

网关启动后，可用 `scripts/showcase.py` 一次串起六项能力：模型路由、流式 TTFT、结构化输出、模板版本、多轮对话、trace/stats；连接 mock 时还会现场演示重试、结构化修复与 429 限流。

```bash
# 真实上游
python scripts/showcase.py --base http://127.0.0.1:8000

# 本地 mock（同时启动 mock 与 demo 网关）
python scripts/showcase.py --base http://127.0.0.1:8010 --mock-url http://127.0.0.1:9001
```

多轮对话客户端：`python scripts/chat_turns.py --model deepseek-v4-pro --stream`。

## 6. 错误码

统一信封：

```json
{"error":{"code":"...","message":"...","type":"...","stage":"...","retryable":false,"request_id":"...","details":{}}}
```

| code | HTTP | stage | 重试 | 触发 |
| --- | --- | --- | --- | --- |
| invalid_request | 422 | request | 否 | 缺字段/未知字段/越界/组合非法 |
| unsupported_parameter_combination | 400 | request | 否 | stream 与结构化同用 |
| invalid_response_format | 422 | request | 否 | 请求 Schema 非法 |
| unsupported_capability | 400 | routing | 否 | 模型不具备所请求能力 |
| model_not_found | 404 | routing | 否 | 未知逻辑模型 |
| auth_failed | 401 | admission | 否 | 网关鉴权失败 |
| rate_limit_exceeded | 429 | admission | 否 | 网关按模型限流（带 Retry-After） |
| template_not_found | 404 | template | 否 | 模板/版本不存在 |
| template_variable_missing | 422 | template | 否 | 缺必填变量 |
| template_render_error | 422 | template | 否 | 未声明/类型非法/超长/静态检查失败 |
| template_conflict | 409 | template | 否 | 同 name+version 重复创建 |
| upstream_connection_error | 502 | connect | 是 | DNS/连接失败/连接重置 |
| upstream_timeout | 504 | connect | 是 | 连接或读取超时 |
| upstream_rate_limited | 503 | upstream_http | 是 | 上游 429 |
| upstream_server_error | 502 | upstream_http | 是 | 上游 500/502/504 |
| upstream_overloaded | 503 | upstream_http | 是 | 上游 503/529 |
| upstream_auth_error | 502 | upstream_http | 否 | 上游 401/403 |
| upstream_insufficient_balance | 502 | upstream_http | 否 | 上游 402 |
| upstream_bad_request | 502 | upstream_http | 否 | 上游 400/404/422 |
| upstream_protocol_error | 502 | protocol | 否 | 200 但不可解析/缺关键字段 |
| upstream_empty_response | 502 | protocol | 否 | 200 但无文本 |
| stream_interrupted | SSE error 帧 | stream | 否 | 流中断/无终止事件 |
| structured_output_invalid | 502 | output_validation | 否 | 修复预算耗尽 |
| structured_output_truncated | 502 | output_validation | 否 | max_tokens 截断 |
| internal_error | 500 | internal | 否 | 未预期异常 |

上游错误体解析失败时退回状态码映射；上游错误摘要脱敏并截断到 300 字符。

## 7. 验证

```bash
python -m pytest                          # 单元测试
python scripts/verify_all.py              # 全量：mock + 真实 live 冒烟（需要真实 DEEPSEEK_API_KEY）
python scripts/verify_all.py --no-live    # 只跑本地 mock（91 项，不需要真实 Key）
python scripts/verify_all.py --upto M4    # 按里程碑
python scripts/verify_all.py --only V-STREAM
```

输出：控制台表格 + `verification_report.md` + `verification_report.json`。报告包含六大能力汇总与每个 V-编号的证据数字。live 冒烟默认开启，缺少真实 Key 时脚本直接报错退出；需要纯离线验证时加 `--no-live`。

## 8. 已知局限

- 限流是**进程内**令牌桶；多 worker 部署时每个进程独立计数。
- trace 队列与后台写入线程在进程内；进程异常退出可能丢失最近少量 trace（正常关机 flush）。
- 不支持工具调用、多模态、会话/`previous_response_id`、自动降级、多租户计费、WebSocket。
- `cost_estimate` 保留字段，本期恒为 null。
- 流式请求使用独立 `httpx.AsyncClient`（保证取消能穿透关闭连接），因此没有跨请求的流式连接复用。
- 模板渲染是单次扫描替换，不支持条件/循环等模板语言。
- Windows 下 `scripts/demo_curl.sh` 需要 Git Bash 或 WSL 的 bash。

## 9. 扩展指南

### 新增协议

1. 在 `src/gateway/providers/` 新增适配器，实现 `Provider.complete()` / `open_stream()` / `aclose()`，只产出 `UnifiedResponse` / `StreamEvent` / `Usage`。
2. 在 `src/gateway/providers/registry.py` 的 `PROTOCOLS` 注册一行。
3. 在 yaml 中给模型配置新 `protocol`。`api` 与 `services` 不需要改动。

### 新增逻辑模型

只改 `config/gateway.yaml`（或验证/演示配置）的 `models`，无需改代码。

### 模板种子

把 `name/version/messages/variables` 的 yaml 放进 `config/prompts/`，启动时幂等导入；同版本内容变化会启动失败而不是静默覆盖。

## 10. 可观测性插件

- 启用：`observability.enabled=true`，配置 `db_path` 与 `queue_size`；启动后 `/admin/observability/traces`、`/traces/{request_id}`、`/stats` 可用。
- 关闭：设为 `false`；业务链路照常工作，admin 路由 404。
- 卸载：删除 `src/gateway/observability/`，并移除 `src/gateway/main.py` 中的 `install_observability` 装配；`core/events.py` 的事件仍会发出，但没有订阅者，行为等价于关闭。

trace 只记录指纹与字符数，不保存完整提示词/输出；上游 Key、鉴权头、堆栈不进入响应、日志或 trace。
