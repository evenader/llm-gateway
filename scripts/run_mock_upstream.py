"""本地 mock 上游启动脚本（README / demo_curl.sh 使用）。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.mock_upstream.app import create_mock_app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本地 mock 上游")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    args = parser.parse_args()
    uvicorn.run(create_mock_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
