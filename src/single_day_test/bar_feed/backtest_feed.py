from __future__ import annotations
from ..domain.enums import BarSource, FeedStatus
from ..domain.errors import BarValidationError
from ..domain.models import CompletedBar, TradingSession
from ..persistence.raw_bar_repository import RawBarRepository
from .bar_validation import validate_complete_backtest_day
from .base import FeedEvent

class BacktestFeed:
    def __init__(self, symbol: str, session: TradingSession, raw_bar_repository: RawBarRepository) -> None:
        self.symbol, self.session, self.repository = symbol, session, raw_bar_repository; self._bars: list[CompletedBar] = []; self._index=0
    def start(self) -> None:
        bars=self.repository.load_rth_bars(self.symbol,self.session.trade_date)
        if not validate_complete_backtest_day(bars,self.session): raise BarValidationError('Local IBAPI raw_1m_bar data is missing or invalid.')
        self._bars=[CompletedBar(raw=b,source=BarSource.HIST) for b in bars]
    def next_event(self) -> FeedEvent:
        if self._index >= len(self._bars): return FeedEvent(FeedStatus.BAR_END,None)
        result=self._bars[self._index]; self._index+=1; return FeedEvent(FeedStatus.BAR_AVAILABLE,result)
    def close(self) -> None: pass
