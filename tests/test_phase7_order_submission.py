from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from single_day_test.application import single_day_runner as runner_module
from single_day_test.application.bar_processor import BarProcessTransition, process_bar as real_process_bar
from single_day_test.application.live_order_submitter import LiveOrderSubmitter
from single_day_test.application.single_day_runner import SingleDayRunner
from single_day_test.bar_feed.base import FeedEvent
from single_day_test.domain.enums import BarSource, DecisionLabel, Direction, FeedStatus, RunMode, ThresholdMode
from single_day_test.domain.errors import IbApiError, PersistenceError
from single_day_test.domain.models import CompletedBar, RawBar, RunContext, SignalEvent, TradingSession
from single_day_test.domain.parameters import ParameterSet
from single_day_test.domain.states import RuntimeState
from single_day_test.ib.config import IbConfig
from single_day_test.ib.gateway import IbApiGateway
from single_day_test.persistence.database import Database, SqliteRepositories

ET = ZoneInfo("America/New_York")


@dataclass
class Clock:
    value: datetime
    def now_et(self) -> datetime: return self.value


class Gateway:
    def __init__(self, *, failures: int = 0) -> None:
        self.connected = False; self.failures = failures; self.submit_error = False; self.orders: list[tuple[str, str, int]] = []; self.connects = 0
    def connect_gateway(self, *, require_account: bool = False) -> None:
        self.connects += 1
        if self.failures:
            self.failures -= 1; raise IbApiError("connect failed")
        assert require_account; self.connected = True
    def disconnect_gateway(self) -> None: self.connected = False
    def is_connected(self) -> bool: return self.connected
    def submit_market_order(self, symbol: str, action: str, quantity: int) -> int:
        if not self.connected: raise IbApiError("not connected")
        if self.submit_error: raise IbApiError("local placeOrder failure")
        self.orders.append((symbol, action, quantity)); return len(self.orders)


def test_order_submitter_retries_and_consumes_only_normally_returned_order() -> None:
    gateway = Gateway(failures=2)
    submitter = LiveOrderSubmitter(gateway, (2, 3))
    submitter.start()
    assert gateway.connects == 3
    assert submitter.submit("AAPL", Direction.BUY, raise_on_error=True)
    assert gateway.orders == [("AAPL", "BUY", 2)]
    assert submitter.current_quantity == 3
    gateway.connected = False
    assert submitter.submit("AAPL", Direction.BUY, raise_on_error=False)
    assert gateway.orders[-1] == ("AAPL", "BUY", 3)
    assert not submitter.submit("AAPL", Direction.BUY, raise_on_error=False)


def test_local_submission_error_preserves_current_share_quantity() -> None:
    gateway = Gateway(); submitter = LiveOrderSubmitter(gateway, (2,))
    submitter.start(); gateway.submit_error = True
    assert not submitter.submit("AAPL", Direction.BUY, raise_on_error=False)
    assert submitter.current_quantity == 2


def test_gateway_builds_accounted_day_market_order_and_keeps_id_sequences_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = IbApiGateway(IbConfig("127.0.0.1", 7497, 73, 1.0))
    gateway.nextValidId(50)
    gateway.managedAccounts("DU123")
    monkeypatch.setattr(gateway, "is_connected", lambda: True)
    captured: list[object] = []
    monkeypatch.setattr(gateway, "placeOrder", lambda order_id, contract, order: captured.extend([order_id, contract, order]))
    request_id, _ = gateway._new_request()
    order_id = gateway.submit_market_order("AAPL", "BUY", 2)
    assert request_id == 1 and order_id == 50
    assert captured[0] == 50
    assert captured[1].exchange == "SMART" and captured[1].currency == "USD"
    assert captured[2].orderType == "MKT" and captured[2].tif == "DAY"
    assert captured[2].account == "DU123" and captured[2].transmit is True


def test_gateway_rejects_zero_or_multiple_managed_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = IbApiGateway(IbConfig("127.0.0.1", 7497, 73, 1.0))
    gateway.nextValidId(50); gateway.managedAccounts("")
    monkeypatch.setattr(gateway, "is_connected", lambda: True)
    with pytest.raises(IbApiError, match="exactly one managed account"):
        gateway.submit_market_order("AAPL", "BUY", 1)
    gateway.managedAccounts("DU1,DU2")
    with pytest.raises(IbApiError, match="exactly one managed account"):
        gateway.submit_market_order("AAPL", "BUY", 1)


class Feed:
    def __init__(self, events: list[FeedEvent]) -> None: self.events = events; self.closed = False
    def start(self) -> None: pass
    def next_event(self) -> FeedEvent: return self.events.pop(0)
    def wait_for_change(self, timeout: float | None = None) -> None: pass
    def clear_error(self) -> None: pass
    def close(self) -> None: self.closed = True


class Submitter:
    def __init__(self) -> None: self.calls: list[tuple[str, Direction]] = []; self.current_quantity = 1; self.remaining_shares = ()
    def recover_after_first_bar(self) -> bool: return True
    def submit(self, symbol: str, direction: Direction, *, raise_on_error: bool) -> bool:
        self.calls.append((symbol, direction)); return True


def params() -> ParameterSet: return ParameterSet("p1", 3, 3, 0.8, 95.0, 95.0, 1)


def context(clock: Clock) -> RunContext:
    return RunContext("phase7", "AAPL", date(2025, 1, 2), params(), Direction.BUY, ThresholdMode.FIXED, 100.0, RunMode.LIVE_PAPER, None, clock.now_et())


def bar(index: int, source: BarSource | None) -> CompletedBar:
    timestamp = datetime(2025, 1, 2, 9, 30, tzinfo=ET) + timedelta(minutes=index)
    return CompletedBar(RawBar("AAPL", int(timestamp.timestamp()), 100, 101, 99, 100, 1, 100, 1), source)


def force_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    def wrapped(*args: object) -> BarProcessTransition:
        result = real_process_bar(*args)  # type: ignore[arg-type]
        signal = SignalEvent(result.record.run_id, result.record.timestamp_et, DecisionLabel.BUY, result.record.close, 1)
        return BarProcessTransition(result.record, replace(result.next_state_after_persist, signal_events=[*result.next_state_after_persist.signal_events, signal]), signal)
    monkeypatch.setattr(runner_module, "process_bar", wrapped)


def test_runner_classifies_at_consumption_and_submits_live_signal_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 33, tzinfo=ET))
    session = TradingSession(date(2025, 1, 2), True, datetime(2025, 1, 2, 9, 30, tzinfo=ET), datetime(2025, 1, 2, 9, 34, tzinfo=ET))
    database = Database(tmp_path / "phase7.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    submitter = Submitter(); force_signal(monkeypatch)
    feed = Feed([FeedEvent(FeedStatus.BAR_AVAILABLE, bar(0, None)), FeedEvent(FeedStatus.BAR_AVAILABLE, bar(2, None)), FeedEvent(FeedStatus.BAR_END, None)])
    summary = SingleDayRunner(database, repos, clock).execute_run(context(clock), feed, RuntimeState.empty(params(), 100), session=session, order_submitter=submitter)
    assert summary.processed_bar_count == 2
    assert submitter.calls == [("AAPL", Direction.BUY)]
    assert [row[0] for row in database.connection.execute("SELECT bar_source FROM processed_1m_bar ORDER BY date")] == ["HIST", "LIVE"]


def test_post_order_persistence_failure_advances_state_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 33, tzinfo=ET))
    database = Database(tmp_path / "phase7-failure.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    submitter = Submitter(); force_signal(monkeypatch)
    original_insert = repos.insert
    def fail_second(value: object) -> None:
        timestamp = getattr(value, "timestamp_et", None)
        if timestamp is not None and timestamp.minute == 31:
            raise PersistenceError("sqlite failed after order")
        original_insert(value)  # type: ignore[arg-type]
    monkeypatch.setattr(repos, "insert", fail_second)
    feed = Feed([FeedEvent(FeedStatus.BAR_AVAILABLE, bar(0, BarSource.HIST)), FeedEvent(FeedStatus.BAR_AVAILABLE, bar(1, BarSource.LIVE)), FeedEvent(FeedStatus.BAR_END, None)])
    with pytest.raises(PersistenceError, match="sqlite failed after order"):
        SingleDayRunner(database, repos, clock).execute_run(context(clock), feed, RuntimeState.empty(params(), 100), order_submitter=submitter)
    assert submitter.calls == [("AAPL", Direction.BUY)]
    assert database.connection.execute("SELECT COUNT(*) FROM processed_1m_bar").fetchone()[0] == 1


def test_recovery_replay_upserts_processed_bar_and_preserves_single_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 33, tzinfo=ET))
    database = Database(tmp_path / "recovery.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    run_context = context(clock); repos.create(run_context)
    existing = SignalEvent(run_context.run_id, bar(0, BarSource.HIST).raw.timestamp_et, DecisionLabel.BUY, 100, 1, 10, (10, 10))
    with database.transaction():
        repos.insert(existing)
    force_signal(monkeypatch)
    feed = Feed([FeedEvent(FeedStatus.BAR_AVAILABLE, bar(0, BarSource.HIST)), FeedEvent(FeedStatus.BAR_END, None)])

    SingleDayRunner(database, repos, clock).execute_run(
        run_context, feed, RuntimeState.empty(params(), 100), create_run=False, recovery=True,
    )

    assert database.connection.execute("SELECT COUNT(*) FROM processed_1m_bar").fetchone()[0] == 1
    assert database.connection.execute("SELECT COUNT(*) FROM signal_event").fetchone()[0] == 1
    assert repos.latest_remaining_shares(run_context.run_id) == (10, 10)
