from __future__ import annotations

from datetime import date

from ..bar_feed.bar_validation import validate_complete_backtest_day
from ..domain.errors import HistoricalDataError, NonTradingDayError
from ..domain.models import RawBar, TradingSession
from ..persistence.raw_bar_repository import RawBarRepository
from ..persistence.trade_date_repository import TradeDateRepository
from .gateway import IbGateway


class TradingSessionService:
    def __init__(self, repository: TradeDateRepository, gateway: IbGateway) -> None:
        self.repository, self.gateway = repository, gateway

    def resolve(self, symbol: str, trade_date: date) -> TradingSession:
        session = self.repository.get(trade_date)
        if session is None:
            session = self.gateway.query_trading_session(symbol, trade_date)
            self.repository.save(session)
        if not session.is_trading_day:
            raise NonTradingDayError(f"{trade_date.isoformat()} is not an RTH trading day")
        return session


class HistoricalBarService:
    BAR_SIZE = "1 min"
    WHAT_TO_SHOW = "TRADES"
    USE_RTH = True

    def __init__(self, repository: RawBarRepository, gateway: IbGateway) -> None:
        self.repository, self.gateway = repository, gateway

    def load_or_fetch(self, symbol: str, session: TradingSession) -> list[RawBar]:
        cached = self.repository.load_rth_bars(symbol, session.trade_date)
        if validate_complete_backtest_day(cached, session):
            return cached
        if session.session_start_et is None or session.session_end_et is None:
            raise HistoricalDataError("Trading session has no RTH boundaries")
        bars = self.gateway.request_historical_1m_bars(symbol, session.session_start_et, session.session_end_et)
        if not validate_complete_backtest_day(bars, session):
            raise HistoricalDataError("IBAPI did not return one complete valid RTH 1-minute day")
        self.repository.upsert_many(bars, bar_size=self.BAR_SIZE, what_to_show=self.WHAT_TO_SHOW, use_rth=self.USE_RTH)
        return bars
