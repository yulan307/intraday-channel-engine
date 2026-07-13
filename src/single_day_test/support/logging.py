from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .clock import Clock


class StructuredLogger(Protocol):
    def info(self, event: str, **fields: object) -> None:
        ...

    def error(self, event: str, **fields: object) -> None:
        ...


class JsonLineLogger:
    """Append one ET-timestamped structured event per line."""

    def __init__(self, path: str | Path, clock: Clock) -> None:
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def info(self, event: str, **fields: object) -> None:
        self._write("INFO", event, fields)

    def error(self, event: str, **fields: object) -> None:
        self._write("ERROR", event, fields)

    def _write(self, level: str, event: str, fields: dict[str, object]) -> None:
        record = {
            "timestamp": self.clock.now_et().isoformat(),
            "level": level,
            "event": event,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, default=str, sort_keys=True))
            stream.write("\n")
