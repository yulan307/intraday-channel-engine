from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from single_day_test.support.ids import DefaultIdGenerator


ET = ZoneInfo("America/New_York")


def test_default_id_generator_uses_start_time_symbol_parameter_set_and_random_suffix() -> None:
    started_at = datetime(2026, 7, 12, 15, 30, 45, tzinfo=ET)

    run_id = DefaultIdGenerator().new_run_id(started_at, "AAPL", "phase-1")

    assert re.fullmatch(r"20260712-153045_AAPL_phase-1_[A-Za-z0-9]{3}", run_id)
