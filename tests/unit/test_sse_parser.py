from __future__ import annotations

from gateway.providers.sse_parser import SSEParser


def _collect(text: str, chunk_size: int = 1) -> list[tuple[str | None, str]]:
    parser = SSEParser()
    raw = text.encode("utf-8")
    events = []
    for index in range(0, len(raw), chunk_size):
        events.extend(parser.feed(raw[index : index + chunk_size]))
    events.extend(parser.close())
    return [(event.event, event.data) for event in events]


def test_multiline_and_crlf_and_comment() -> None:
    payload = (
        "event: e1\r\ndata: line1\r\ndata: line2\r\n\r\n"
        ": keep-alive\n\n"
        "event: e2\ndata: 你好\n\n"
    )
    expected = [("e1", "line1\nline2"), ("e2", "你好")]
    for size in (1, 2, 3, 5, len(payload)):
        assert _collect(payload, size) == expected


def test_glued_events_and_tail_flush() -> None:
    payload = "event: a\ndata: 1\n\nevent: b\ndata: 2\n\ndata: tail"
    assert _collect(payload, 1) == [("a", "1"), ("b", "2"), (None, "tail")]


def test_multibyte_split_across_chunks() -> None:
    payload = "event: x\ndata: 中文内容\n\n"
    assert _collect(payload, 1) == [("x", "中文内容")]


def test_id_and_retry_fields() -> None:
    parser = SSEParser()
    events = parser.feed(b"id: 42\nretry: 1000\nevent: ping\ndata: {}\n\n")
    assert len(events) == 1
    assert events[0].id == "42"
    assert events[0].retry == 1000
    assert events[0].event == "ping"


def test_pure_comment_yields_nothing() -> None:
    parser = SSEParser()
    assert parser.feed(b": keep-alive\n\n") == []
    assert parser.close() == []
