from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.wrapper import EWrapper
from ibapi import server_versions

from ..domain.errors import HistoricalDataError, IbApiError
from ..domain.models import RawBar, TradingSession
from ..support.logging import StructuredLogger
from .config import IbConfig

ET = ZoneInfo("America/New_York")


class SubscriptionHandle(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True)
class LiveBarCallbacks:
    historical: Callable[[RawBar], None]
    historical_end: Callable[[], None]
    update: Callable[[RawBar], None]
    error: Callable[[Exception], None]


class IbGateway(Protocol):
    def query_trading_session(self, symbol: str, trade_date: date) -> TradingSession: ...
    def request_historical_1m_bars(self, symbol: str, start_et: datetime, end_et: datetime) -> list[RawBar]: ...
    def subscribe_completed_1m_bars(self, symbol: str, callback: Callable[[RawBar], None]) -> SubscriptionHandle: ...
    def start_live_1m_bars(self, symbol: str, duration_seconds: int, callbacks: LiveBarCallbacks) -> SubscriptionHandle: ...


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    bars: list[RawBar] = field(default_factory=list)
    schedule: Sequence[object] | None = None
    error: Exception | None = None
    symbol: str = ""


class _LiveSubscription:
    def __init__(self, gateway: "IbApiGateway", request_id: int) -> None:
        self.gateway, self.request_id, self.closed = gateway, request_id, False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.gateway._close_live_request(self.request_id)


class IbApiGateway(EWrapper, EClient):  # type: ignore[misc]
    """Blocking domain gateway over IBAPI's callback-driven client."""
    def __init__(self, config: IbConfig, logger: StructuredLogger | None = None) -> None:
        EClient.__init__(self, self)
        self.config = config
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._next_request_id = 1
        self._next_order_id: int | None = None
        self._pending: dict[int, _PendingRequest] = {}
        self._live_callbacks: dict[int, tuple[str, LiveBarCallbacks]] = {}
        self._connection_error: Exception | None = None
        self._accounts_ready = threading.Event()
        self._managed_accounts: tuple[str, ...] = ()
        self._logger = logger

    def set_logger(self, logger: StructuredLogger | None) -> None:
        self._logger = logger

    def _info(self, event: str, **fields: object) -> None:
        if self._logger is not None:
            self._logger.info(event, **fields)

    def _error(self, event: str, **fields: object) -> None:
        if self._logger is not None:
            self._logger.error(event, **fields)

    def connect_gateway(self, *, require_account: bool = False) -> None:
        self._ensure_schedule_client_version()
        if self.isConnected():
            if require_account:
                self._await_single_account()
            return
        self._ready.clear()
        self._accounts_ready.clear()
        self._managed_accounts = ()
        self._connection_error = None
        self._info("ibapi_connecting", host=self.config.host, port=self.config.port, client_id=self.config.client_id)
        self.connect(self.config.host, self.config.port, self.config.client_id)
        self._thread = threading.Thread(target=self.run, name="ibapi-event-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.config.connect_timeout):
            self.disconnect()
            raise IbApiError("Timed out waiting for IBAPI nextValidId; verify TWS API connection and client ID")
        if require_account:
            self._await_single_account()
        self._info("ibapi_connected", client_id=self.config.client_id)

    def _await_single_account(self) -> None:
        if not self._accounts_ready.wait(self.config.connect_timeout):
            raise IbApiError("Timed out waiting for IBAPI managedAccounts callback")
        if len(self._managed_accounts) != 1:
            raise IbApiError(
                "Expected exactly one managed account, got "
                f"{len(self._managed_accounts)}: {', '.join(self._managed_accounts) or 'none'}"
            )

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
        self._accounts_ready.clear()
        self._managed_accounts = ()

    def is_connected(self) -> bool:
        return self.isConnected() and self._ready.is_set()

    def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IBAPI callback spelling
        with self._lock:
            self._next_order_id = max(self._next_order_id or orderId, orderId)
        self._ready.set()
        self._info("ibapi_next_valid_id", order_id=orderId)

    def managedAccounts(self, accountsList: str) -> None:  # noqa: N802 - IBAPI callback spelling
        accounts = tuple(account.strip() for account in accountsList.split(",") if account.strip())
        with self._lock:
            self._managed_accounts = accounts
        self._accounts_ready.set()
        self._info("ibapi_managed_accounts", accounts=list(accounts), account_count=len(accounts))

    def submit_market_order(self, symbol: str, action: str, quantity: int) -> int:
        if action not in {"BUY", "SELL"}:
            raise IbApiError(f"Unsupported order action: {action}")
        if quantity < 1:
            raise IbApiError("Order quantity must be at least one")
        if not self.is_connected():
            raise IbApiError("IBAPI order gateway is not connected")
        self._await_single_account()
        with self._lock:
            if self._next_order_id is None:
                raise IbApiError("IBAPI nextValidId is unavailable for order submission")
            order_id = self._next_order_id
            self._next_order_id += 1
            account = self._managed_accounts[0]
        order = Order()
        order.action = action
        order.orderType = "MKT"
        order.totalQuantity = quantity
        order.tif = "DAY"
        order.account = account
        order.transmit = True
        self._info(
            "ibapi_order_submitting",
            order_id=order_id,
            symbol=symbol,
            action=action,
            quantity=quantity,
            account=account,
            order_type=order.orderType,
            tif=order.tif,
        )
        self.placeOrder(order_id, self._stock(symbol), order)
        return order_id

    def error(self, reqId: int, errorTime: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
        self._error(
            "ibapi_error_callback", request_id=reqId, error_time=errorTime,
            error_code=errorCode, error_message=errorString,
            advanced_order_reject_json=advancedOrderRejectJson or None,
        )
        if errorCode in {2104, 2106, 2158}:
            return
        error = IbApiError(f"IBAPI error {errorCode} for request {reqId}: {errorString}")
        with self._lock:
            pending = self._pending.get(reqId)
        if pending is not None:
            pending.error = error
            pending.event.set()
            return
        with self._lock:
            live = self._live_callbacks.get(reqId)
        if live is not None:
            self._fail_live(reqId, error)
        elif errorCode in {1100, 1300, 502}:
            self._connection_error = error
            for item in tuple(self._pending.values()):
                item.error = error
                item.event.set()
            self._fail_all_live(error)

    def historicalData(self, reqId: int, bar: object) -> None:  # noqa: N802
        with self._lock:
            live = self._live_callbacks.get(reqId)
        if live is not None:
            symbol, callbacks = live
            try:
                self._info("ibapi_historical_callback", request_id=reqId, symbol=symbol)
                callbacks.historical(self._raw_bar(symbol, bar))
            except Exception as exc:
                self._fail_live(reqId, exc)
            return
        with self._lock:
            pending = self._pending.get(reqId)
        if pending is not None:
            try:
                self._info("ibapi_historical_callback", request_id=reqId, symbol=pending.symbol)
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
        self._fail_all_live(error)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802
        with self._lock:
            live = self._live_callbacks.get(reqId)
        if live is not None:
            try:
                self._info("ibapi_historical_end", request_id=reqId)
                live[1].historical_end()
            except Exception as exc:
                self._fail_live(reqId, exc)
            return
        self._complete(reqId)

    def historicalDataUpdate(self, reqId: int, bar: object) -> None:  # noqa: N802
        with self._lock:
            live = self._live_callbacks.get(reqId)
        if live is None:
            return
        symbol, callbacks = live
        try:
            self._info("ibapi_historical_update", request_id=reqId, symbol=symbol)
            callbacks.update(self._raw_bar(symbol, bar))
        except Exception as exc:
            self._fail_live(reqId, exc)

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
        self._info("ibapi_schedule_requested", request_id=request_id, symbol=symbol, trade_date=trade_date.isoformat())
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
        self._info("ibapi_historical_requested", request_id=request_id, symbol=symbol, start_et=start_et.isoformat(), end_et=end_et.isoformat())
        self.reqHistoricalData(request_id, self._stock(symbol), end, "1 D", "1 min", "TRADES", 1, 2, False, [])
        self._await(request_id, pending)
        return [bar for bar in pending.bars if start_et <= bar.timestamp_et < end_et]

    def subscribe_completed_1m_bars(self, symbol: str, callback: Callable[[RawBar], None]) -> SubscriptionHandle:
        raise NotImplementedError("Live subscriptions begin in Phase 4")

    def start_live_1m_bars(self, symbol: str, duration_seconds: int, callbacks: LiveBarCallbacks) -> SubscriptionHandle:
        if duration_seconds <= 0:
            raise IbApiError("Live historical duration must be positive")
        request_id, _ = self._new_request()
        with self._lock:
            self._pending.pop(request_id, None)
            self._live_callbacks[request_id] = (symbol, callbacks)
        self._info("ibapi_live_historical_requested", request_id=request_id, symbol=symbol, duration_seconds=duration_seconds)
        self.reqHistoricalData(request_id, self._stock(symbol), "", f"{duration_seconds} S", "1 min", "TRADES", 1, 2, True, [])
        return _LiveSubscription(self, request_id)

    def _close_live_request(self, request_id: int) -> None:
        self.cancelHistoricalData(request_id)
        with self._lock:
            self._live_callbacks.pop(request_id, None)

    def _fail_live(self, request_id: int, error: Exception) -> None:
        with self._lock:
            item = self._live_callbacks.pop(request_id, None)
        if item is not None:
            self.cancelHistoricalData(request_id)
            item[1].error(error)

    def _fail_all_live(self, error: Exception) -> None:
        with self._lock:
            request_ids = tuple(self._live_callbacks)
        for request_id in request_ids:
            self._fail_live(request_id, error)

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
