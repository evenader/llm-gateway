"""多轮对话客户端：客户端维护完整历史，每轮打印回答与 trace 摘要。

用法（网关已在运行，默认 http://127.0.0.1:8000）：
  python scripts/chat_turns.py --model deepseek-v4-pro --turns "你好，我叫小林" "我叫什么？"
  python scripts/chat_turns.py --model deepseek-v4-flash --stream
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx


def fetch_trace(base: str, request_id: str, timeout: float = 2.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base}/admin/observability/traces/{request_id}", timeout=5)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            return None
        time.sleep(0.05)
    return None


def print_trace(base: str, request_id: str | None) -> None:
    if not request_id:
        return
    trace = fetch_trace(base, request_id)
    if trace is None:
        print(f"  trace: {request_id}（暂不可用：插件关闭或尚未落库）")
        return
    print(
        "  trace:"
        f" status={trace['status']}"
        f" latency={trace['latency_total_ms']}ms"
        f" ttft={trace['ttft_ms']}"
        f" tokens={trace['input_tokens']}/{trace['output_tokens']}/{trace['total_tokens']}"
        f" retry={trace['retry_count']}"
        + (f" error={trace['error_code']}" if trace.get("error_code") else "")
    )


def print_error(response: httpx.Response) -> None:
    try:
        body = response.json()
    except ValueError:
        body = response.text[:500]
    print(f"请求失败 HTTP {response.status_code}: {json.dumps(body, ensure_ascii=False)}")


def complete(base: str, model: str, messages: list[dict[str, str]], args) -> tuple[str, str | None] | None:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
    }
    try:
        response = httpx.post(
            f"{base}/v1/chat/completions", json=payload, timeout=args.timeout
        )
    except httpx.HTTPError as exc:
        print(f"连接网关失败: {type(exc).__name__}: {exc}")
        return None
    if response.status_code != 200:
        print_error(response)
        return None
    body = response.json()
    answer = body["choices"][0]["message"]["content"]
    usage = body.get("usage") or {}
    request_id = body.get("gateway", {}).get("request_id") or response.headers.get("x-request-id")
    print(answer)
    print(
        f"  usage: prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}"
        f" total={usage.get('total_tokens')} request_id={request_id} retry={body.get('gateway', {}).get('retry_count')}"
    )
    return answer, request_id


def stream(base: str, model: str, messages: list[dict[str, str]], args) -> tuple[str, str | None] | None:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "stream": True,
    }
    answer = ""
    request_id = None
    final_usage: dict[str, Any] = {}
    try:
        with httpx.stream(
            "POST", f"{base}/v1/chat/completions", json=payload, timeout=args.timeout
        ) as response:
            request_id = response.headers.get("x-request-id")
            if response.status_code != 200:
                raw = response.read()
                print(f"请求失败 HTTP {response.status_code}: {raw.decode(errors='replace')[:500]}")
                return None
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
                if "error" in chunk:
                    print(f"流式错误: {json.dumps(chunk, ensure_ascii=False)}")
                    return None
                choice = (chunk.get("choices") or [{}])[0]
                delta = (choice.get("delta") or {}).get("content")
                if delta:
                    answer += delta
                    print(delta, end="", flush=True)
                if choice.get("finish_reason") is not None:
                    final_usage = chunk.get("usage") or {}
                    request_id = chunk.get("gateway", {}).get("request_id") or request_id
    except httpx.HTTPError as exc:
        print(f"流式连接失败: {type(exc).__name__}: {exc}")
        return None
    print()
    print(
        f"  usage: prompt={final_usage.get('prompt_tokens')} completion={final_usage.get('completion_tokens')}"
        f" total={final_usage.get('total_tokens')} request_id={request_id}"
    )
    if not answer:
        print("  （未收到任何文本）")
    return answer, request_id


def run_turn(base: str, model: str, history: list[dict[str, str]], question: str, args) -> bool:
    history.append({"role": "user", "content": question})
    print(f"\n你: {question}")
    print("模型: ", end="", flush=True)
    result = (
        stream(base, model, history, args)
        if args.stream
        else complete(base, model, history, args)
    )
    if result is None:
        history.pop()
        return False
    answer, request_id = result
    history.append({"role": "assistant", "content": answer})
    print_trace(base, request_id)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Gateway 多轮对话客户端")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--system", default=None, help="可选的 system 消息")
    parser.add_argument("--turns", nargs="*", help="依次发送的问题；省略则进入交互模式")
    parser.add_argument("--stream", action="store_true", help="使用流式输出")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    history: list[dict[str, str]] = []
    if args.system:
        history.append({"role": "system", "content": args.system})

    if args.turns:
        for question in args.turns:
            if not run_turn(base, args.model, history, question, args):
                sys.exit(1)
        return

    print("进入交互模式：输入问题回车发送，输入 exit / quit 结束。")
    while True:
        try:
            question = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in ("exit", "quit", "q"):
            break
        if not question:
            continue
        run_turn(base, args.model, history, question, args)


if __name__ == "__main__":
    main()
