from __future__ import annotations

from typing import Protocol
from datetime import date

from ..domain.models import TradingSession


class TradeDateRepository(Protocol):
    def get(self, trade_date: date) -> TradingSession | None:
        ...

    def save(self, session: TradingSession) -> None:
        ...
