from __future__ import annotations

from typing import Protocol
from datetime import datetime

from ..domain.models import RunContext


class RunRepository(Protocol):
    def create(self, context: RunContext) -> None:
        ...

    def mark_completed(self, run_id: str, ended_at_et: datetime) -> None:
        ...

    def mark_failed(
        self, run_id: str, ended_at_et: datetime, error_type: str, error_message: str
    ) -> None:
        ...
