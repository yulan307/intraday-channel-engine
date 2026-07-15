from __future__ import annotations

from typing import Protocol

from ..domain.models import SignalEvent


class SignalRepository(Protocol):
    def insert(self, event: SignalEvent) -> None:
        ...

    def latest_remaining_shares(self, run_id: str) -> tuple[int, ...] | None:
        ...
