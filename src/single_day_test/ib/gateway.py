from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper
from ibapi import server_versions

from ..domain.errors import HistoricalDataError, IbApiError
from ..domain.models import RawBar, TradingSession
from .config import IbConfig

ET = ZoneInfo("America/New_York")


class SubscriptionHandle(Protocol):
    def close(self) -> None: ...


class IbGateway(Protocol):
    def query_trading_session(self, symbol: str, trade_date: date) -> TradingSession: ...
    def request_historical_1m_bars(self, symbol: str, start_et: datetime, end_et: datetime) -> list[RawBar]: ...
    def subscribe_completed_1m_bars(self, symbol: str, callback: Callable[[RawBar], None]) -> SubscriptionHandle: ...


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    bars: list[RawBar] = field(default_factory=list)
    schedule: Sequence[object] | None = None
    error: Exception | None = None
    symbol: str = ""


class IbApiGateway(EWrapper, EClient):  # type: ignore[misc]
    """Blocking domain gateway over IBAPI's callback-driven client."""
    def __init__(self, config: IbConfig) -> None:
        EClient.__init__(self, self)
        self.config = config
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._next_request_id = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._connection_error: Exception | None = None

    def connect_gateway(self) -> None:
        self._ensure_schedule_client_version()
        if self.isConnected():
            return
        self.connect(self.config.host, self.config.port, self.config.client_id)
        self._thread = threading.Thread(target=self.run, name="ibapi-event-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.config.connect_timeout):
            self.disconnect()
            raise IbApiError("Timed out waiting for IBAPI nextValidId; verify TWS API connection and client ID")

    @staticmethod
    def _ensure_schedule_client_version() -> None:
        required = getattr(server_versions, "MIN_SERVER_VER_HISTORICAL_SCHEDULE", 165)
        supported = getattr(server_versions, "MAX_CLIENT_VER", 0)
        if supported < required:
            raise IbApiError(
                f"Installed ibapi client protocol {supported} cannot request SCHEDULE (requires {required}). "
                "Install the official IBKR TWS API 10.12+ Python client; do not use the PyPI 9.81 package."
            )

    def disconnect_gateway(self) -> None:
        self.disconnect()
        if self._thread is not None:
            self._thread.join(timeout=self.config.connect_timeout)
            self._thread = None
        self._ready.clear()

    def is_connected(self) -> bool:
        return self.isConnected() and self._ready.is_set()

    def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IBAPI callback spelling
        with self._lock:
            self._next_request_id = max(self._next_request_id, orderId)
        self._ready.set()

    def error(self, reqId: int, errorTime: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
        if errorCode in {2104, 2106, 2158}:
            return
        error = IbApiError(f"IBAPI error {errorCode} for request {reqId}: {errorString}")
        with self._lock:
            pending = self._pending.get(reqId)
        if pending is not None:
            pending.error = error
            pending.event.set()
        elif errorCode in {1100, 1300, 502}:
            self._connection_error = error
            for item in tuple(self._pending.values()):
                item.error = error
                item.event.set()

    def historicalData(self, reqId: int, bar: object) -> None:  # noqa: N802
        with self._lock:
            pending = self._pending.get(reqId)
        if pending is not None:
            try:
                pending.bars.append(self._raw_bar(pending.symbol, bar))
            except HistoricalDataError as exc:
                pending.error = exc
                pending.event.set()

    def connectionClosed(self) -> None:  # noqa: N802
        error = IbApiError("TWS closed the IBAPI connection")
        self._connection_error = error
        for pending in tuple(self._pending.values()):
            pending.error = error
            pending.event.set()

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802
        self._complete(reqId)

    def historicalSchedule(self, reqId: int, startDateTime: str, endDateTime: str, timeZone: str, sessions: Sequence[object]) -> None:  # noqa: N802
        with self._lock:
            pending = self._pending.get(reqId)
        if pending is not None:
            pending.schedule = sessions
        self._complete(reqId)

    def query_trading_session(self, symbol: str, trade_date: date) -> TradingSession:
        request_id, pending = self._new_request()
        pending.symbol = symbol
        end = f"{trade_date.isoformat().replace('-', '')} 23:59:59 US/Eastern"
        self.reqHistoricalData(request_id, self._stock(symbol), end, "1 D", "1 day", "SCHEDULE", 1, 1, False, [])
        self._await(request_id, pending)
        sessions = list(pending.schedule or [])
        matching = [item for item in sessions if getattr(item, "refDate", "") == trade_date.isoformat().replace("-", "")]
        if not matching:
            return TradingSession(trade_date, False, None, None)
        selected = matching[0]
        return TradingSession(trade_date, True, self._schedule_datetime(str(getattr(selected, "startDateTime"))), self._schedule_datetime(str(getattr(selected, "endDateTime"))))

    def request_historical_1m_bars(self, symbol: str, start_et: datetime, end_et: datetime) -> list[RawBar]:
        request_id, pending = self._new_request()
        pending.symbol = symbol
        end = end_et.astimezone(ET).strftime("%Y%m%d %H:%M:%S US/Eastern")
        self.reqHistoricalData(request_id, self._stock(symbol), end, "1 D", "1 min", "TRADES", 1, 2, False, [])
        self._await(request_id, pending)
        return [bar for bar in pending.bars if start_et <= bar.timestamp_et < end_et]

    def subscribe_completed_1m_bars(self, symbol: str, callback: Callable[[RawBar], None]) -> SubscriptionHandle:
        raise NotImplementedError("Live subscriptions begin in Phase 4")

    def _new_request(self) -> tuple[int, _PendingRequest]:
        if not self.is_connected():
            raise IbApiError("IBAPI gateway is not connected")
        if self._connection_error is not None:
            raise self._connection_error
        with self._lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            pending = _PendingRequest()
            self._pending[request_id] = pending
        return request_id, pending

    def _await(self, request_id: int, pending: _PendingRequest) -> None:
        if not pending.event.wait(self.config.connect_timeout):
            self.cancelHistoricalData(request_id)
            with self._lock: self._pending.pop(request_id, None)
            raise HistoricalDataError(f"Timed out waiting for IBAPI request {request_id}")
        with self._lock: self._pending.pop(request_id, None)
        if pending.error is not None:
            raise pending.error

    def _complete(self, request_id: int) -> None:
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is not None:
            pending.event.set()

    @staticmethod
    def _stock(symbol: str) -> Contract:
        contract = Contract(); contract.symbol = symbol; contract.secType = "STK"; contract.exchange = "SMART"; contract.currency = "USD"
        return contract

    @staticmethod
    def _raw_bar(symbol: str, bar: object) -> RawBar:
        raw_date = getattr(bar, "date")
        try:
            epoch = int(raw_date)
        except (TypeError, ValueError) as exc:
            raise HistoricalDataError(f"Expected IBAPI formatDate=2 epoch date, got {raw_date!r}") from exc
        return RawBar(symbol, epoch, float(getattr(bar,"open")), float(getattr(bar,"high")), float(getattr(bar,"low")), float(getattr(bar,"close")), float(getattr(bar,"volume")), float(getattr(bar,"wap")), int(getattr(bar,"barCount")))

    @staticmethod
    def _schedule_datetime(value: str) -> datetime:
        return datetime.strptime(value, "%Y%m%d-%H:%M:%S").replace(tzinfo=ET)
