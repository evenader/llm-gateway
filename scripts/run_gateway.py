"""启动网关（供 README / demo_curl.sh 使用）。

用法：
  python scripts/run_gateway.py --config config/gateway.demo.yaml --port 8010
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway.main import create_app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 LLM Gateway")
    parser.add_argument("--config", default=str(ROOT / "config" / "gateway.yaml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    app = create_app(args.config)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
