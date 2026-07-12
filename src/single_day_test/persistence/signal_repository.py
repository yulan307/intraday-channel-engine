from __future__ import annotations

from typing import Protocol

from ..domain.models import SignalEvent


class SignalRepository(Protocol):
    def insert(self, event: SignalEvent) -> None:
        ...
