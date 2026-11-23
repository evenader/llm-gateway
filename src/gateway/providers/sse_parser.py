"""增量 SSE 解析器：不依赖 chunk 边界，支持多字节字符切分、CRLF/LF、多行 data、注释行。"""
from __future__ import annotations

import codecs
import re
from dataclasses import dataclass

_DELIMITER = re.compile(r"\r\n\r\n|\n\n|\r\r")
_LINE = re.compile(r"\r\n|\n|\r")


@dataclass(frozen=True)
class SSEEvent:
    event: str | None
    data: str
    id: str | None = None
    retry: int | None = None


class SSEParser:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        text = self._decoder.decode(chunk)
        if not text:
            return []
        self._buffer += text
        return self._drain(final=False)

    def close(self) -> list[SSEEvent]:
        text = self._decoder.decode(b"", final=True)
        if text:
            self._buffer += text
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[SSEEvent]:
        events: list[SSEEvent] = []
        while True:
            match = _DELIMITER.search(self._buffer)
            if match is None:
                break
            raw = self._buffer[: match.start()]
            self._buffer = self._buffer[match.end() :]
            event = self._parse(raw)
            if event is not None:
                events.append(event)
        if final:
            remainder, self._buffer = self._buffer, ""
            if remainder.strip():
                event = self._parse(remainder)
                if event is not None:
                    events.append(event)
        return events

    @staticmethod
    def _parse(raw: str) -> SSEEvent | None:
        event_name: str | None = None
        data_lines: list[str] = []
        event_id: str | None = None
        retry: int | None = None
        for line in _LINE.split(raw):
            if not line or line.startswith(":"):
                continue
            field, sep, value = line.partition(":")
            if not sep:
                value = ""
            elif value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value)
            elif field == "id":
                if "\x00" not in value:
                    event_id = value
            elif field == "retry":
                try:
                    retry = int(value)
                except ValueError:
                    retry = None
        if not data_lines and event_name is None:
            return None
        return SSEEvent(event=event_name, data="\n".join(data_lines), id=event_id, retry=retry)
