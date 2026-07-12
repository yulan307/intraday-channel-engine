from __future__ import annotations
from datetime import timedelta
from math import isfinite
from collections.abc import Sequence
from ..domain.errors import BarValidationError
from ..domain.models import RawBar, TradingSession

def validate_raw_bar(bar: RawBar) -> None:
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    if not all(isfinite(x) for x in values) or min(bar.open,bar.high,bar.low,bar.close) <= 0 or bar.volume < 0:
        raise BarValidationError('Raw Bar OHLC must be finite and positive; volume must be finite and non-negative')
    if bar.high < max(bar.open,bar.close,bar.low) or bar.low > min(bar.open,bar.close):
        raise BarValidationError('Raw Bar OHLC relationship is invalid')

def validate_complete_backtest_day(bars: Sequence[RawBar], session: TradingSession) -> bool:
    try:
        if not session.is_trading_day or session.session_start_et is None or session.session_end_et is None: return False
        expected=[]; current=session.session_start_et
        while current < session.session_end_et: expected.append(current); current += timedelta(minutes=1)
        if len(bars) != len(expected): return False
        timestamps=[bar.timestamp_et for bar in bars]
        if timestamps != expected or len(set(timestamps)) != len(timestamps): return False
        for bar in bars: validate_raw_bar(bar)
        return True
    except BarValidationError: return False
