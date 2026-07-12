from __future__ import annotations

import secrets
import string
from datetime import datetime
from typing import Protocol


class IdGenerator(Protocol):
    def new_run_id(
        self, started_at_local: datetime, symbol: str, parameter_set_id: str
    ) -> str:
        ...


class DefaultIdGenerator:
    """Generate human-readable run identifiers for a single-day execution."""

    def new_run_id(
        self, started_at_local: datetime, symbol: str, parameter_set_id: str
    ) -> str:
        suffix = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(3))
        timestamp = started_at_local.strftime("%Y%m%d-%H%M%S")
        return f"{timestamp}_{symbol}_{parameter_set_id}_{suffix}"
