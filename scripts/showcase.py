"""网关能力现场演示：路由、流式 TTFT、结构化输出、模板版本、多轮、可观测、故障注入。

用法（网关已启动）：
  python scripts/showcase.py --base http://127.0.0.1:8000
  python scripts/showcase.py --base http://127.0.0.1:8010 --mock-url http://127.0.0.1:9001
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Any

import httpx

MODELS: list[str] = []


def section(title: str) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def chat(base: str, payload: dict[str, Any], timeout: float = 180.0) -> tuple[int, Any]:
    try:
        response = httpx.post(f"{base}/v1/chat/completions", json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text


def stream_chat(base: str, payload: dict[str, Any], timeout: float = 180.0) -> dict[str, Any]:
    text = ""
    request_id = None
    first_delta_ms = None
    started = time.perf_counter()
    final: dict[str, Any] = {}
    try:
        with httpx.stream("POST", f"{base}/v1/chat/completions", json=payload, timeout=timeout) as response:
            request_id = response.headers.get("x-request-id")
            if response.status_code != 200:
                return {"error": response.read().decode(errors="replace")[:300]}
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                delta = (choice.get("delta") or {}).get("content")
                if delta:
                    if first_delta_ms is None:
                        first_delta_ms = (time.perf_counter() - started) * 1000
                    text += delta
                    print(delta, end="", flush=True)
                if choice.get("finish_reason") is not None:
                    final = chunk
    except httpx.HTTPError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    print()
    return {
        "text": text,
        "request_id": request_id,
        "client_first_delta_ms": round(first_delta_ms, 1) if first_delta_ms else None,
        "client_total_ms": round((time.perf_counter() - started) * 1000, 1),
        "usage": final.get("usage") or {},
        "gateway": final.get("gateway") or {},
    }


def trace_of(base: str, request_id: str | None) -> dict[str, Any] | None:
    if not request_id:
        return None
    for _ in range(40):
        try:
            response = httpx.get(f"{base}/admin/observability/traces/{request_id}", timeout=5)
        except httpx.HTTPError:
            return None
        if response.status_code == 200:
            return response.json()
        time.sleep(0.05)
    return None


def set_mock(mock_url: str, body: dict[str, Any]) -> None:
    httpx.post(f"{mock_url}/__control", json=body, timeout=5)


def show_models(base: str) -> None:
    section("1. 统一模型表与路由")
    response = httpx.get(f"{base}/v1/models", timeout=10)
    payload = response.json()
    global MODELS
    MODELS = [item["id"] for item in payload.get("data", [])]
    for item in payload.get("data", []):
        print(f"- {item['id']:20} protocol={item['protocol']:20} capabilities={item['capabilities']}")

    question = "用一句话解释什么是幂等性。"
    for model in ("deepseek-v4-pro", "deepseek-v4-flash"):
        started = time.perf_counter()
        status, body = chat(
            base, {"model": model, "messages": [{"role": "user", "content": question}], "max_tokens": 96}
        )
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        if status != 200:
            print(f"{model}: 失败 {status} {str(body)[:200]}")
            continue
        gateway = body["gateway"]
        usage = body["usage"]
        answer = body["choices"][0]["message"]["content"].replace("\n", " ")[:80]
        print(
            f"{model}: {elapsed}ms protocol={gateway['protocol']} upstream={gateway['upstream_model']}"
            f" tokens={usage['prompt_tokens']}/{usage['completion_tokens']}/{usage['total_tokens']}"
        )
        print(f"  → {answer}...")


def show_stream(base: str) -> None:
    section("2. 流式输出与 TTFT")
    result = stream_chat(
        base,
        {
            "model": "deepseek-v4-pro",
            "stream": True,
            "messages": [{"role": "user", "content": "用 5 句话介绍后端开发，每句一行。"}],
            "max_tokens": 200,
        },
    )
    if "error" in result:
        print("流式失败:", result["error"])
        return
    trace = trace_of(base, result["request_id"])
    print(
        f"客户端首块 {result['client_first_delta_ms']}ms / 总耗时 {result['client_total_ms']}ms"
        f" | usage={result['usage']}"
    )
    if trace:
        print(
            f"网关 trace: ttft={trace['ttft_ms']}ms generation={trace['generation_ms']}ms"
            f" total={trace['latency_total_ms']}ms status={trace['status']}"
        )


def show_structured(base: str, mock_url: str | None) -> None:
    section("3. 结构化输出（json_schema）")
    schema = {
        "type": "object",
        "required": ["title", "bullets"],
        "properties": {
            "title": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}, "minItems": 3},
        },
        "additionalProperties": False,
    }
    if mock_url:
        fake = json.dumps(
            {"title": "网关能力", "bullets": ["流式", "结构化", "模板", "重试", "限流", "可观测"]},
            ensure_ascii=False,
        )
        set_mock(mock_url, {"responses": {"text": fake}, "anthropic": {"text": fake}})
    response = chat(
        base,
        {
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "把网关的六大能力总结成 JSON，title + 至少 3 条 bullets。"}],
            "max_tokens": 300,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "summary", "schema": schema, "strict": True},
            },
        },
    )
    status, body = response
    if status != 200:
        print("结构化请求失败:", status, str(body)[:300])
        return
    message = body["choices"][0]["message"]
    print("parsed:", json.dumps(message.get("parsed"), ensure_ascii=False))
    print("repair_count:", body["gateway"]["repair_count"], "usage:", body["usage"])
    if mock_url:
        set_mock(mock_url, {"responses": {}, "anthropic": {}})


def show_templates(base: str) -> None:
    section("4. 提示词模板与版本")
    for version in ("1.0.0", "1.1.0"):
        response = httpx.post(
            f"{base}/v1/prompts/code-review/versions/{version}/render",
            json={"variables": {"code": "def add(a, b): return a + b"}},
            timeout=10,
        )
        if response.status_code != 200:
            print(f"渲染 {version} 失败:", response.text[:200])
            continue
        body = response.json()
        print(f"{version}: chars={body['chars']} fingerprint={body['fingerprint'][:16]}...")
        print("  system:", body["messages"][0]["content"].replace("\n", " ")[:90], "...")

    payload = {
        "name": "showcase-greet",
        "version": "1.0.0",
        "description": "showcase",
        "owner": "demo",
        "changelog": "initial",
        "messages": [
            {"role": "system", "content": "You are {{ role }}."},
            {"role": "user", "content": "Say hello to {{ name }} in one short sentence."},
        ],
        "variables": [
            {"name": "role", "required": False, "default": "assistant"},
            {"name": "name", "required": True},
        ],
    }
    created = httpx.post(f"{base}/v1/prompts", json=payload, timeout=10)
    print("创建模板:", created.status_code, "(409 表示已存在，可继续)")

    status, body = chat(
        base,
        {
            "model": "deepseek-v4-flash",
            "messages": None,
            "max_tokens": 80,
            "prompt": {"name": "showcase-greet", "version": "latest", "variables": {"name": "小林"}},
        },
    )
    if status == 200:
        print("模板调用:", body["choices"][0]["message"]["content"])
        print("gateway.prompt:", body["gateway"]["prompt"])
        trace = trace_of(base, body["gateway"]["request_id"])
        if trace:
            print("trace.prompt:", trace["prompt_name"], trace["prompt_version"], "chars=", trace["prompt_chars"])


def show_multi_turn(base: str) -> None:
    section("5. 多轮对话（客户端维护历史）")
    history: list[dict[str, str]] = []
    for question in ("你好，我叫小林，正在学后端。", "我叫什么？在学什么？", "把对话总结成三点。"):
        history.append({"role": "user", "content": question})
        status, body = chat(base, {"model": "deepseek-v4-pro", "messages": history, "max_tokens": 200})
        if status != 200:
            print("多轮失败:", status, str(body)[:300])
            return
        answer = body["choices"][0]["message"]["content"]
        usage = body["usage"]
        print(f"\nQ: {question}")
        print(f"A: {answer[:150]}")
        print(
            f"  input_tokens={usage['prompt_tokens']} output_tokens={usage['completion_tokens']}"
            f" request_id={body['gateway']['request_id']}"
        )
        history.append({"role": "assistant", "content": answer})


def show_observability(base: str) -> None:
    section("6. 可观测数据")
    traces = httpx.get(f"{base}/admin/observability/traces?limit=6", timeout=10).json()["data"]
    print("最近 6 条 trace:")
    for item in traces:
        print(
            f"- {item['request_id'][:8]} model={item['logical_model']} status={item['status']}"
            f" latency={item['latency_total_ms']}ms ttft={item['ttft_ms']}"
            f" tokens={item['input_tokens']}/{item['output_tokens']}/{item['total_tokens']}"
            f" retry={item['retry_count']} prompt={item['prompt_name']}@{item['prompt_version']}"
        )
    stats = httpx.get(f"{base}/admin/observability/stats", timeout=10).json()
    print("\n按模型聚合:")
    for model, data in stats["models"].items():
        p95_ttft = data["p95_ttft_ms"]
        p95_text = f"{p95_ttft}ms" if p95_ttft is not None else "null"
        print(
            f"- {model}: requests={data['requests']} success_rate={data['success_rate']}"
            f" total_tokens={data['total_tokens']} avg_latency={data['avg_latency_ms']}ms"
            f" p95_ttft={p95_text} retry={data['retry_total']}"
            f" errors={data['error_counts']}"
        )


def show_faults(base: str, mock_url: str) -> None:
    section("7. 故障注入：重试 / 结构化修复 / 限流（mock 模式）")
    set_mock(
        mock_url,
        {"responses": {"sequence": [{"kind": "status", "status": 503}, {"kind": "status", "status": 503}, {"kind": "ok"}]}},
    )
    status, body = chat(base, {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64})
    if status == 200:
        gateway = body["gateway"]
        print(f"重试演示: retry_count={gateway['retry_count']}（503 → 503 → 成功）")
        trace = trace_of(base, gateway["request_id"])
        if trace:
            print("  attempts:", [(a["index"], a["error_code"], a["backoff_ms"]) for a in trace["attempts"]])
            print("  retry_wait_ms:", trace["retry_wait_ms"])

    set_mock(
        mock_url,
        {
            "responses": {
                "sequence": [
                    {"text": '{"ok": 1', "finish": "stop"},
                    {"text": '{"ok": true}', "finish": "stop"},
                ]
            }
        },
    )
    status, body = chat(
        base,
        {
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "give json"}],
            "max_tokens": 64,
            "response_format": {"type": "json_object"},
        },
    )
    if status == 200:
        print(
            "结构化修复: repair_count=", body["gateway"]["repair_count"],
            "usage_total=", body["usage"]["total_tokens"],
            "parsed=", body["choices"][0]["message"]["parsed"],
        )

    rate_model = "demo-limited" if "demo-limited" in MODELS else "deepseek-v4-flash"
    set_mock(mock_url, {"responses": {}, "anthropic": {}})
    codes: list[int] = []
    for _ in range(14):
        status, _body = chat(base, {"model": rate_model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16})
        codes.append(status)
    print(
        f"限流演示 model={rate_model}: 200×{codes.count(200)} 429×{codes.count(429)}"
        f" other={[c for c in codes if c not in (200, 429)][:3]}"
    )
    print("  429 表示网关按模型限流（进程内令牌桶），与上游 429 的 503 映射不同")


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Gateway 能力演示")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--mock-url", default=None, help="本地 mock 控制面地址，例如 http://127.0.0.1:9001")
    parser.add_argument("--skip-rate", action="store_true", help="跳过限流演示（真实上游时减少调用）")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    try:
        print("健康检查:", httpx.get(f"{base}/healthz", timeout=5).json())
    except httpx.HTTPError as exc:
        print(f"无法连接网关 {base}: {exc}")
        return

    show_models(base)
    show_stream(base)
    show_structured(base, args.mock_url)
    show_templates(base)
    show_multi_turn(base)
    show_observability(base)
    if args.mock_url:
        if args.skip_rate:
            print("\n（已跳过限流演示）")
        else:
            show_faults(base, args.mock_url)
    else:
        print("\n提示：连接本地 mock 并传 --mock-url 可现场观察重试、结构化修复与 429 限流。")


if __name__ == "__main__":
    main()
