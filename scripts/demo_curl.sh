#!/usr/bin/env bash
# 在本地 mock 配置下实跑 README 中的 curl 示例。
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python}"
if [ -x ".venv/Scripts/python.exe" ]; then
  PYTHON=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
fi

export DEMO_UPSTREAM_KEY="${DEMO_UPSTREAM_KEY:-demo-local-key}"
mkdir -p data/demo
rm -f data/demo/prompts.db* data/demo/observability.db*

API="http://127.0.0.1:8010"
MOCK="http://127.0.0.1:9001"

cleanup() {
  kill "${GW_PID:-}" "${MOCK_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT

"$PYTHON" scripts/run_mock_upstream.py --port 9001 >data/demo/mock.log 2>&1 &
MOCK_PID=$!
"$PYTHON" scripts/run_gateway.py --config config/gateway.demo.yaml --port 8010 >data/demo/gateway.log 2>&1 &
GW_PID=$!

for _ in $(seq 1 120); do
  if curl -sf "$API/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
curl -sf "$API/healthz" >/dev/null

section() { printf '\n===== %s =====\n' "$1"; }

section "健康检查"
curl -s "$API/healthz"; echo

section "模型列表"
curl -s "$API/v1/models"; echo

section "非流式 pro"
curl -s -X POST "$API/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}],"max_tokens":128}'; echo

section "非流式 flash"
curl -s -X POST "$API/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}],"max_tokens":128}'; echo

section "流式 pro"
curl -sN -X POST "$API/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","stream":true,"messages":[{"role":"user","content":"hi"}],"max_tokens":128}'

section "流式 flash"
curl -sN -X POST "$API/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","stream":true,"messages":[{"role":"user","content":"hi"}],"max_tokens":128}'

section "json_object"
curl -s -X POST "$API/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"give json"}],"max_tokens":128,"response_format":{"type":"json_object"}}'; echo

section "json_schema"
curl -s -X POST "$API/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"give json"}],"max_tokens":128,"response_format":{"type":"json_schema","json_schema":{"name":"demo","schema":{"type":"object","required":["ok"],"properties":{"ok":{"type":"boolean"}}},"strict":true}}}'; echo

section "创建模板"
curl -s -X POST "$API/v1/prompts" \
  -H 'Content-Type: application/json' \
  -d '{"name":"greet","version":"1.0.0","description":"demo template","owner":"demo","changelog":"initial","messages":[{"role":"system","content":"You are {{ role }}."},{"role":"user","content":"Say hello to {{ name }}"}],"variables":[{"name":"role","required":false,"default":"assistant"},{"name":"name","required":true}]}'; echo

section "按版本引用模板"
curl -s -X POST "$API/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-pro","max_tokens":128,"prompt":{"name":"code-review","version":"1.1.0","variables":{"code":"print(1)"}}}'; echo

section "render 预览"
curl -s -X POST "$API/v1/prompts/code-review/versions/latest/render" \
  -H 'Content-Type: application/json' \
  -d '{"variables":{"code":"print(1)"}}'; echo

section "触发 429（demo-limited burst=2）"
for _ in 1 2 3 4; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$API/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"demo-limited","messages":[{"role":"user","content":"hi"}],"max_tokens":64}'
done

section "traces"
curl -s "$API/admin/observability/traces?limit=3"; echo

section "stats"
curl -s "$API/admin/observability/stats"; echo

section "验证脚本（完整）"
"$PYTHON" scripts/verify_all.py >data/demo/verify.log 2>&1
tail -n 15 data/demo/verify.log

printf '\nDemo 完成。日志：data/demo/mock.log / data/demo/gateway.log / data/demo/verify.log\n'
