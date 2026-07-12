from __future__ import annotations

from typing import Protocol

from ..domain.models import ProcessedBarRecord


class ProcessedBarRepository(Protocol):
    def insert(self, record: ProcessedBarRecord) -> None:
        ...
