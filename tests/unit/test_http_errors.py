from __future__ import annotations

import httpx

from gateway.core.errors import GatewayError, sanitize
from gateway.providers.http_errors import map_httpx_error, map_status


def test_status_mapping_table() -> None:
    cases = {
        400: ("upstream_bad_request", 502, False),
        401: ("upstream_auth_error", 502, False),
        402: ("upstream_insufficient_balance", 502, False),
        403: ("upstream_auth_error", 502, False),
        404: ("upstream_bad_request", 502, False),
        422: ("upstream_bad_request", 502, False),
        429: ("upstream_rate_limited", 503, True),
        500: ("upstream_server_error", 502, True),
        502: ("upstream_server_error", 502, True),
        503: ("upstream_overloaded", 503, True),
        504: ("upstream_server_error", 502, True),
        529: ("upstream_overloaded", 503, True),
    }
    for status, (code, http, retryable) in cases.items():
        error = map_status(status)
        assert (error.code, error.http_status, error.retryable) == (code, http, retryable)


def test_status_mapping_parses_error_message_and_retry_after() -> None:
    body = b'{"error": {"message": "quota exceeded"}}'
    error = map_status(429, body, {"Retry-After": "2"})
    assert error.code == "upstream_rate_limited"
    assert error.details["retry_after_s"] == 2.0
    assert "quota exceeded" in error.message


def test_httpx_exception_mapping() -> None:
    timeout = map_httpx_error(httpx.ReadTimeout("slow"))
    assert timeout.code == "upstream_timeout"
    assert timeout.retryable is True

    connect = map_httpx_error(httpx.ConnectError("refused"))
    assert connect.code == "upstream_connection_error"
    assert connect.retryable is True


def test_sanitize_removes_keys_and_truncates() -> None:
    text = "Authorization: Bearer abc.def sk-live-1234567890 " + "Z" * 400
    cleaned = sanitize(text)
    assert "abc.def" not in cleaned
    assert "sk-live" not in cleaned
    assert len(cleaned) <= 300


def test_gateway_error_envelope() -> None:
    error = GatewayError("rate_limit_exceeded", "too many", details={"model": "m"})
    envelope = error.envelope("rid")["error"]
    assert envelope["code"] == "rate_limit_exceeded"
    assert envelope["type"] == "rate_limit_error"
    assert envelope["retryable"] is False
    assert envelope["request_id"] == "rid"
