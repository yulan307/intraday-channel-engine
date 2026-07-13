from __future__ import annotations

from typing import Protocol
from datetime import datetime
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now_et(self) -> datetime:
        ...


class SystemClock:
    def now_et(self) -> datetime:
        return datetime.now(ZoneInfo("America/New_York"))
