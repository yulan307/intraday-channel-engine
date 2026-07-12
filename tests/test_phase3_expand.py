from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from single_day_test.application.backtest_cli import parse_scan_request
from single_day_test.application.backtest_scan import BacktestScanner
from single_day_test.application.bar_processor import process_bar
from single_day_test.bar_feed.base import FeedEvent
from single_day_test.domain.enums import BarSource, DecisionLabel, Direction, FeedStatus, RunMode, RunStatus, ThresholdMode
from single_day_test.domain.errors import InputValidationError, NonTradingDayError
from single_day_test.domain.models import CompletedBar, RawBar, RunContext
from single_day_test.domain.parameters import ParameterSet
from single_day_test.domain.states import RuntimeState
from single_day_test.engine.channel_engine import ChannelEngine
from single_day_test.engine.decision_engine import DecisionEngine
from single_day_test.engine.trend_engine import TrendEngine
from single_day_test.persistence.database import Database, SqliteRepositories


ET = ZoneInfo("America/New_York")


def _params() -> ParameterSet:
    return ParameterSet("p1", 3, 3, 1, 1, 0.8, 95.0, 95.0, 1, 1)


def _bar(index: int, price: float = 100.0) -> CompletedBar:
    timestamp = datetime(2025, 1, 2, 9, 30, tzinfo=ET) + timedelta(minutes=index)
    return CompletedBar(RawBar("AAPL", int(timestamp.timestamp()), price, price + 1, price - 1, price, 1, price, 1), BarSource.HIST)


def _context(mode: ThresholdMode) -> RunContext:
    return RunContext("run-1", "AAPL", date(2025, 1, 2), _params(), Direction.BUY, mode, 100.0 if mode is ThresholdMode.FIXED else None, RunMode.BACKTEST, None, datetime(2025, 1, 2, 9, 0, tzinfo=ET))


def test_auto_threshold_warmup_then_uses_nth_bar_price() -> None:
    context = _context(ThresholdMode.AUTO)
    state = RuntimeState.empty(context.parameter_set)
    records = []
    for index in range(3):
        transition = process_bar(context, _bar(index, 100.0 + index), state, TrendEngine(), ChannelEngine(), DecisionEngine())
        records.append(transition.record)
        state = transition.next_state_after_persist
    assert [record.active_threshold for record in records] == [None, None, 102.0]
    assert [record.decision.decision for record in records[:2]] == [DecisionLabel.NO_BUY, DecisionLabel.NO_BUY]


def test_parse_scan_request_enforces_dates_threshold_and_generated_run_id() -> None:
    payload = {
        "symbol": "AAPL", "direction": "SELL", "trade_date_start": "2025-01-02",
        "trade_date_end": "2025-01-03", "threshold": 0,
        "parameter_set": {"path": "configs/parameter_set.csv", "parameter_set_id": ""},
    }
    request, selected_id, path = parse_scan_request(payload, date(2025, 1, 3))
    assert request.threshold_mode is ThresholdMode.FIXED and request.fixed_threshold == 0.0
    assert request.trade_dates == (date(2025, 1, 2), date(2025, 1, 3))
    assert selected_id == "" and path == Path("configs/parameter_set.csv")
    with pytest.raises(InputValidationError):
        parse_scan_request({**payload, "run_id": "forbidden"}, date(2025, 1, 3))
    with pytest.raises(InputValidationError):
        parse_scan_request({**payload, "threshold": ""}, date(2025, 1, 3))
    with pytest.raises(InputValidationError):
        parse_scan_request({**payload, "trade_date_end": "2025-01-04"}, date(2025, 1, 3))


class _Feed:
    def __init__(self) -> None:
        self.events = [_bar(0), _bar(1)]

    def start(self) -> None:
        pass

    def next_event(self) -> FeedEvent:
        if self.events:
            return FeedEvent(FeedStatus.BAR_AVAILABLE, self.events.pop(0))
        return FeedEvent(FeedStatus.BAR_END, None)

    def close(self) -> None:
        pass


class _FailingFeed(_Feed):
    def next_event(self) -> FeedEvent:
        if self.events:
            return super().next_event()
        raise RuntimeError("feed failed after first bars")


class _Ids:
    def new_run_id(self, started_at_local: datetime, symbol: str, parameter_set_id: str) -> str:
        return f"run-{parameter_set_id}"


def test_scanner_skips_non_trading_day_and_exports_one_multi_day_csv(tmp_path) -> None:
    database = Database(tmp_path / "expand.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    request = type("Request", (), {
        "symbol": "AAPL", "direction": Direction.BUY,
        "trade_dates": (date(2025, 1, 2), date(2025, 1, 3)),
        "threshold_mode": ThresholdMode.FIXED, "fixed_threshold": 100.0,
    })()

    def feed_factory(_: str, trade_date: date):
        if trade_date == date(2025, 1, 3):
            raise NonTradingDayError("closed")
        return _Feed()

    scanner = BacktestScanner(database, repositories, _Ids(), datetime(2025, 1, 2, 9, 0, tzinfo=ET), feed_factory, tmp_path / "data")
    summaries = scanner.execute(request, [_params()])
    statuses = {(item.trade_date, item.status) for item in summaries}
    assert (date(2025, 1, 2), RunStatus.COMPLETED) in statuses
    assert (date(2025, 1, 3), RunStatus.SKIPPED) in statuses
    assert (tmp_path / "data" / "run-p1.csv").exists()
    rows = database.connection.execute("SELECT run_id, trade_date, status FROM single_day_run ORDER BY trade_date").fetchall()
    assert [(row["run_id"], row["trade_date"], row["status"]) for row in rows] == [("run-p1", "2025-01-02", "COMPLETED"), ("run-p1", "2025-01-03", "SKIPPED")]


def test_schema_shape_mismatch_is_rebuilt_and_processed_fields_are_exact(tmp_path) -> None:
    path = tmp_path / "shape.sqlite3"
    database = Database(path)
    database.initialize()
    database.connection.execute("ALTER TABLE processed_1m_bar ADD COLUMN unexpected TEXT")
    database.connection.commit()
    database.initialize()
    columns = [row[1] for row in database.connection.execute("PRAGMA table_info(processed_1m_bar)")]
    assert "unexpected" not in columns
    assert "initial_threshold" not in columns
    assert columns[17] == "active_threshold"


def test_failed_day_keeps_partial_processed_rows_in_final_csv(tmp_path) -> None:
    database = Database(tmp_path / "expand.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    request = type("Request", (), {
        "symbol": "AAPL", "direction": Direction.BUY,
        "trade_dates": (date(2025, 1, 2),),
        "threshold_mode": ThresholdMode.FIXED, "fixed_threshold": 100.0,
    })()
    scanner = BacktestScanner(
        database, repositories, _Ids(), datetime(2025, 1, 2, 9, 0, tzinfo=ET),
        lambda _symbol, _date: _FailingFeed(), tmp_path / "data",
    )
    scanner.execute(request, [_params()])
    status = database.connection.execute("SELECT status FROM single_day_run WHERE run_id='run-p1'").fetchone()[0]
    rows = database.connection.execute("SELECT COUNT(*) FROM processed_1m_bar WHERE run_id='run-p1'").fetchone()[0]
    assert status == "FAILED" and rows == 2
    assert sum(1 for _ in (tmp_path / "data" / "run-p1.csv").open(encoding="utf-8")) == 3
