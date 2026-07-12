from __future__ import annotations

from typing import Protocol


class StructuredLogger(Protocol):
    def info(self, event: str, **fields: object) -> None:
        ...

    def error(self, event: str, **fields: object) -> None:
        ...
