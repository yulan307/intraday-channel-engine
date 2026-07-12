from __future__ import annotations

from typing import Callable, Protocol
from datetime import date, datetime

from ..domain.models import TradingSession, RawBar


class SubscriptionHandle(Protocol):
    def close(self) -> None:
        ...


class IbGateway(Protocol):
    def query_trading_session(
        self, symbol: str, trade_date: date
    ) -> TradingSession:
        ...

    def request_historical_1m_bars(
        self, symbol: str, start_et: datetime, end_et: datetime
    ) -> list[RawBar]:
        ...

    def subscribe_completed_1m_bars(
        self, symbol: str, callback: Callable[[RawBar], None]
    ) -> SubscriptionHandle:
        ...
