from __future__ import annotations

from typing import Protocol
from datetime import date, datetime

from ..domain.models import RunContext
from ..domain.models import RunSummary


class RunRepository(Protocol):
    def create(self, context: RunContext) -> None:
        ...

    def mark_completed(self, run_id: str, trade_date: date, ended_at_et: datetime) -> None:
        ...

    def mark_failed(
        self, run_id: str, trade_date: date, ended_at_et: datetime, error_type: str, error_message: str
    ) -> None:
        ...

    def complete_with_summary(self, summary: RunSummary, *, write_run_summary: bool = True) -> None:
        ...

    def fail_with_summary(self, summary: RunSummary, *, write_run_summary: bool = True) -> None:
        ...
