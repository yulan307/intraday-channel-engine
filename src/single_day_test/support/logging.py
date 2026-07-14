from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .clock import Clock


class StructuredLogger(Protocol):
    @property
    def trace_enabled(self) -> bool:
        ...

    def info(self, event: str, **fields: object) -> None:
        ...

    def error(self, event: str, **fields: object) -> None:
        ...

    def summary(self, event: str, **fields: object) -> None:
        ...

    def stop_info_trace(self) -> None:
        ...


class JsonLineLogger:
    """Append one ET-timestamped structured event per line."""

    def __init__(self, path: str | Path, clock: Clock, log_level: str = "INFO") -> None:
        if log_level not in {"INFO", "ERROR"}:
            raise ValueError(f"Unsupported log level: {log_level}")
        self.path = Path(path)
        self.clock = clock
        self._trace_enabled = log_level == "INFO"

    @property
    def trace_enabled(self) -> bool:
        return self._trace_enabled

    def info(self, event: str, **fields: object) -> None:
        if self._trace_enabled:
            self._write("INFO", event, fields)

    def error(self, event: str, **fields: object) -> None:
        self._write("ERROR", event, fields)

    def summary(self, event: str, **fields: object) -> None:
        self._write("INFO", event, fields)

    def stop_info_trace(self) -> None:
        self._trace_enabled = False

    def _write(self, level: str, event: str, fields: dict[str, object]) -> None:
        record = {
            "timestamp": self.clock.now_et().isoformat(),
            "level": level,
            "event": event,
            **fields,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, default=str, sort_keys=True))
            stream.write("\n")


class TerminalMirrorLogger:
    """Mirror structured file events to the terminal without changing their schema."""

    def __init__(self, logger: StructuredLogger) -> None:
        self.logger = logger

    @property
    def trace_enabled(self) -> bool:
        return self.logger.trace_enabled

    def info(self, event: str, **fields: object) -> None:
        if self.trace_enabled:
            self.logger.info(event, **fields)
            self._print("INFO", event, fields)

    def error(self, event: str, **fields: object) -> None:
        self.logger.error(event, **fields)
        self._print("ERROR", event, fields)

    def summary(self, event: str, **fields: object) -> None:
        self.logger.summary(event, **fields)
        self._print("INFO", event, fields)

    def stop_info_trace(self) -> None:
        self.logger.stop_info_trace()

    @staticmethod
    def _print(level: str, event: str, fields: dict[str, object]) -> None:
        print(f"{level} {event} {json.dumps(fields, default=str, sort_keys=True)}")
