from __future__ import annotations

from typing import Protocol, Sequence
from datetime import date

from ..domain.models import RawBar


class RawBarRepository(Protocol):
    def load_rth_bars(self, symbol: str, trade_date: date) -> list[RawBar]:
        ...

    def upsert_many(self, bars: Sequence[RawBar], *, bar_size: str, what_to_show: str, use_rth: bool) -> None:
        ...
