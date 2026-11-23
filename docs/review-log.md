# Review Log

## M0 蓝图（2026-11-23）

- 改了什么：新增 `docs/blueprint/00-architecture.md`、`01-api-contract.md`、`02-feature-design.md`、`03-error-codes.md`、`04-verification-plan.md`、`05-decisions.md`。
- 影响面：设计阶段，无代码。
- 复查了哪些功能：§5 全部验证编号（F1–F9）已映射到蓝图与验证计划；六大能力均有对应编号。
- 结果：蓝图完成。集中提问：无阻塞性问题。
- 门禁：设计确认后开始 M1。

## M1 骨架（2026-11-23）

- 改了什么：新增 `pyproject.toml`、`.env.example`、`config/gateway.yaml`、`config/gateway.verify.yaml`、`src/gateway/{config,main}.py`、`core/{clock,types,errors,events,retry,ratelimit}.py`、`providers/{base,registry,openai_responses,anthropic_messages,sse_parser,http_errors}.py`、`services/{router,structured,chat_service}.py`、`api/{schemas,sse,routes_chat,routes_misc,routes_sse_stream,error_handlers}.py`、`tests/mock_upstream/app.py`、`scripts/verify_all.py`。
- 影响面：全新代码，无既有功能。
- 复查了哪些功能：入口校验与错误信封、request_id 传递、未知模型、能力校验、非透明代理。
- 结果：pytest 34 passed；`verify_all.py --upto M1` 相关检查通过（V-ERR-02/04/05、V-ROUTE-02/06/07）。

## M2 适配器与 Usage 归一（2026-11-23）

- 改了什么：两个协议适配器（非流式 + 流式事件映射）、Usage 归一、路由、http_errors 表驱动映射、mock 上游协议校验与调用计数。
- 影响面：providers 与 chat_service 的上游调用路径。
- 复查了哪些功能：入口校验、错误信封、路由隔离、非透明请求重建。
- 结果：`verify_all.py --upto M2` 中 V-ROUTE-01/03/05、V-ADP-01~06、V-ERR-01/03/06 全部 PASS；pytest 34 passed。

## M3 重试与限流（2026-11-23）

- 改了什么：`core/retry.py` 接入 chat_service；`core/ratelimit.py` 按模型令牌桶；验证配置新增低 burst、小预算、超时模型。
- 影响面：非流式调用路径、准入阶段。
- 复查了哪些功能：错误映射、非透明代理、错误信封、Usage。
- 结果：`verify_all.py --upto M3` 中 V-RETRY-01~08/10、V-RL-01~06/08、V-ERR-07 全部 PASS；pytest 34 passed。

## M4 流式输出（2026-11-23）

- 改了什么：`sse_parser`、适配器流式事件映射、`StreamSession`（泵/队列/心跳/提交点）、SSE 编码、取消清理、mock 流式场景（首块延迟、块间隔、无终止事件、自定义 usage）。
- 影响面：`providers/base.UpstreamStream`（流式独立 client）、`GatewayMiddleware` 改为纯 ASGI、`chat_service` 流式路径。
- 复查了哪些功能：V-ROUTE-*、V-ADP-*、V-ERR-*、V-RETRY-*、V-RL-* 全部回归。
- 结果：pytest 34 passed；`python scripts/verify_all.py --upto M4` → 48 PASS / 0 FAIL / 1 DEFERRED（V-STREAM-13 TTFT 依赖 trace，M7 转 PASS），六大能力中「两个模型调用」「重试与限流」OK。
- 偏离与回写：流式改用独立 httpx client、取消清理独立 task、纯 ASGI 中间件、trust_env=False，已回写 `00-architecture.md` 与 `05-decisions.md`（D26–D30）。
- 待评审：确认后继续 M5 结构化输出、M6 模板、M7 可观测性。

## M5 结构化输出（2026-11-23）

- 改了什么：结构化请求的 Schema 自校验、语法/字段/业务三层校验、有限修复循环、pro 原生 `text.format` 与 flash 提示词注入两条路径；补齐 V-STRUCT-01~11 验证。
- 影响面：`services/structured.py`、`services/chat_service.py` 的结构化分支、`providers/*` 的 `response_format` 构造。
- 复查了哪些功能：V-ROUTE-*、V-ADP-*、V-RETRY-04/10、V-STREAM-12 回归。
- 结果：`verify_all.py --upto M5` → 59 PASS / 0 FAIL / 2 DEFERRED（trace 依赖项）；V-STRUCT 11 项全部 PASS；pytest 40 passed（新增模板单测前为 34）。
- 备注：markdown 围栏不会被静默去壳；顶层数组在 json_object 下视为非法；修复预算与网络重试预算独立。

## M6 提示词模板（2026-11-23）

- 改了什么：`services/prompts.py`（SQLite 模板库、SemVer、单次扫描渲染、静态校验、种子幂等）、`api/routes_prompts.py`、`config/prompts/*.yaml`；ChatService 接入模板渲染并写入 trace 字段。
- 影响面：请求准备阶段与 `/v1/prompts` 端点；模板错误全部在调用上游前返回。
- 复查了哪些功能：chat 校验、结构化、流式（模板 + stream、模板 + json_object）。
- 结果：`verify_all.py --upto M6` → 71 PASS / 0 FAIL / 2 DEFERRED（V-STREAM-13、V-TPL-11 依赖 M7）；种子冲突启动失败、PUT/DELETE 405、注入文本不二次渲染均通过。

## M7 可观测性（2026-11-23）

- 改了什么：`observability/`（EventBus 订阅、Recorder 有界队列 + 后台批量写、WAL SQLite、traces/stats 路由）、`main.py` 插件装配与关机 flush；trace 相关检查全部转 PASS。
- 影响面：组合根与事件消费；业务层零 import 保持。
- 复查了哪些功能：全部 chat 路径（成功/失败/拒绝/取消）、重试计数、限流拒绝、模板 trace 字段。
- 结果：`verify_all.py --upto M7` → 88 PASS / 0 FAIL / 0 DEFERRED；trace 字段、对账、隐私、故障隔离、重启持久化全部通过。

## M8 收尾（2026-11-23）

- 改了什么：README、`config/gateway.demo.yaml`、`scripts/run_gateway.py`、`scripts/demo_curl.sh`、`.gitignore`、prompt 单测；M8 静态检查与 live SKIP。
- 影响面：文档与演示脚本，无业务逻辑变更。
- 复查了哪些功能：全量回归、demo 实跑、README 与 demo 命令一致性。
- 结果：`pytest` 40 passed；`python scripts/verify_all.py` → 91 PASS / 0 FAIL / 0 DEFERRED / 1 SKIP（V-ADP-08 live）；六大能力汇总全部 OK；`scripts/demo_curl.sh` 在 Git Bash 下实跑通过（含创建模板、429、traces、stats、完整验证）。
- 环境说明：系统无可用 bash（WSL 未安装），demo 使用 Git Bash（`D:\a_cs_environment\path_git\Git\bin\bash.exe`）执行；README 的 curl 与脚本一致。
