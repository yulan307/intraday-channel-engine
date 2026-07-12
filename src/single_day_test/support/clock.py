from __future__ import annotations

from typing import Protocol
from datetime import datetime


class Clock(Protocol):
    def now_et(self) -> datetime:
        ...
