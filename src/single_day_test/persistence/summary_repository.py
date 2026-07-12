from __future__ import annotations

from typing import Protocol

from ..domain.models import RunSummary


class SummaryRepository(Protocol):
    def save(self, summary: RunSummary) -> None:
        ...
