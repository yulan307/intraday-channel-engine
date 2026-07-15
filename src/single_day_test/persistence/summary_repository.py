from __future__ import annotations

from typing import Protocol

class SummaryRepository(Protocol):
    def save_run_summary(self, run_id: str) -> None:
        ...
