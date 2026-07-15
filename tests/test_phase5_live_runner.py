from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from single_day_test.application.single_day_runner import SingleDayRunner
from single_day_test.bar_feed.base import FeedEvent
from single_day_test.domain.enums import BarSource, Direction, FeedStatus, RunMode, RunStatus, ThresholdMode
from single_day_test.domain.errors import PersistenceError
from single_day_test.domain.models import CompletedBar, RawBar, RunContext
from single_day_test.domain.parameters import ParameterSet
from single_day_test.domain.states import RuntimeState
from single_day_test.persistence.database import Database, SqliteRepositories
from single_day_test.support.logging import JsonLineLogger

ET = ZoneInfo("America/New_York")


@dataclass
class Clock:
    value: datetime

    def now_et(self) -> datetime:
        return self.value


class Feed:
    def __init__(self, events: list[FeedEvent]) -> None:
        self.events = events
        self.closed = False
        self.waited = 0
        self.next_calls = 0

    def start(self) -> None:
        pass

    def next_event(self) -> FeedEvent:
        self.next_calls += 1
        return self.events.pop(0)

    def wait_for_change(self, timeout: float | None = None) -> None:
        assert timeout is None
        self.waited += 1

    def close(self) -> None:
        self.closed = True

    def clear_error(self) -> None:
        pass


class FailingFeed(Feed):
    def __init__(self, events: list[FeedEvent]) -> None:
        super().__init__(events)
        self.failed_once = False

    def next_event(self) -> FeedEvent:
        self.next_calls += 1
        if self.events:
            return self.events.pop(0)
        if not self.failed_once:
            self.failed_once = True
            raise RuntimeError("feed failed")
        return FeedEvent(FeedStatus.BAR_END, None)


def params() -> ParameterSet:
    return ParameterSet("p1", 3, 3, 0.8, 95.0, 95.0, 1)


def context(clock: Clock) -> RunContext:
    return RunContext(
        "phase5-run", "AAPL", date(2025, 1, 2), params(), Direction.BUY,
        ThresholdMode.FIXED, 100.0, RunMode.LIVE_PAPER, None, clock.now_et(),
    )


def bar(index: int, source: BarSource) -> CompletedBar:
    timestamp = datetime(2025, 1, 2, 9, 30, tzinfo=ET) + timedelta(minutes=index)
    raw = RawBar("AAPL", int(timestamp.timestamp()), 100 + index, 101 + index, 99 + index, 100 + index, 1, 100 + index, 1)
    return CompletedBar(raw, source)


def test_live_runner_processes_hist_live_end_and_writes_jsonl(tmp_path: Path) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 33, tzinfo=ET))
    database = Database(tmp_path / "phase5.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    feed = Feed([
        FeedEvent(FeedStatus.BAR_WAITING, None),
        FeedEvent(FeedStatus.BAR_AVAILABLE, bar(0, BarSource.HIST)),
        FeedEvent(FeedStatus.BAR_AVAILABLE, bar(1, BarSource.LIVE)),
        FeedEvent(FeedStatus.BAR_AVAILABLE, bar(2, BarSource.END)),
        FeedEvent(FeedStatus.BAR_END, None),
    ])
    logger = JsonLineLogger(tmp_path / "logs" / "phase5-run.jsonl", clock)

    summary = SingleDayRunner(database, repositories, clock, logger).execute_run(
        context(clock), feed, RuntimeState.empty(params(), 100.0),
    )

    assert summary.status is RunStatus.COMPLETED
    assert feed.waited == 1 and feed.closed
    assert database.connection.execute("SELECT status FROM single_day_run").fetchone()[0] == "COMPLETED"
    daily_statistics = database.connection.execute("SELECT processed_bar_count, signal_count, best_reward, efficiency FROM single_day_run").fetchone()
    assert tuple(daily_statistics) == (3, 0, None, None)
    assert [row[0] for row in database.connection.execute("SELECT bar_source FROM processed_1m_bar ORDER BY date")] == ["HIST", "LIVE", "END"]
    assert database.connection.execute("SELECT status FROM run_summary").fetchone()[0] == "COMPLETED"
    events = [json.loads(line)["event"] for line in (tmp_path / "logs" / "phase5-run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events == ["bar_received", "bar_analysis_completed", "bar_persisted", "first_bar_confirmed", "run_completed"]


def test_live_runner_recovers_from_post_first_bar_feed_error(tmp_path: Path) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 33, tzinfo=ET))
    database = Database(tmp_path / "phase5-failure.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    feed = FailingFeed([FeedEvent(FeedStatus.BAR_AVAILABLE, bar(0, BarSource.HIST))])
    logger = JsonLineLogger(tmp_path / "logs" / "phase5-run.jsonl", clock)

    summary = SingleDayRunner(database, repositories, clock, logger).execute_run(
        context(clock), feed, RuntimeState.empty(params(), 100.0),
    )

    assert feed.closed
    assert database.connection.execute("SELECT COUNT(*) FROM processed_1m_bar").fetchone()[0] == 1
    assert feed.waited == 1
    assert summary.status is RunStatus.COMPLETED
    assert database.connection.execute("SELECT status FROM single_day_run").fetchone()[0] == "COMPLETED"
    assert database.connection.execute("SELECT status FROM run_summary").fetchone()[0] == "COMPLETED"


def test_error_log_level_keeps_errors_and_summary_after_full_run(tmp_path: Path) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 33, tzinfo=ET))
    database = Database(tmp_path / "error-level.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    feed = Feed([
        FeedEvent(FeedStatus.BAR_AVAILABLE, bar(0, BarSource.HIST)),
        FeedEvent(FeedStatus.BAR_AVAILABLE, bar(1, BarSource.END)),
        FeedEvent(FeedStatus.BAR_END, None),
    ])
    logger = JsonLineLogger(tmp_path / "logs" / "error-level.jsonl", clock, "ERROR")

    summary = SingleDayRunner(database, repositories, clock, logger).execute_run(
        context(clock), feed, RuntimeState.empty(params(), 100.0),
    )

    assert summary.processed_bar_count == 2
    events = [json.loads(line)["event"] for line in (tmp_path / "logs" / "error-level.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events == ["run_completed"]


def test_processed_bar_persistence_failure_stops_before_next_bar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 33, tzinfo=ET))
    database = Database(tmp_path / "phase5-persistence.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    feed = Feed([
        FeedEvent(FeedStatus.BAR_AVAILABLE, bar(0, BarSource.HIST)),
        FeedEvent(FeedStatus.BAR_AVAILABLE, bar(1, BarSource.LIVE)),
    ])
    original_insert = repositories.insert

    def fail_processed(value: object) -> None:
        raise PersistenceError("processed write failed")

    monkeypatch.setattr(repositories, "insert", fail_processed)
    with pytest.raises(PersistenceError, match="processed write failed"):
        SingleDayRunner(database, repositories, clock).execute_run(
            context(clock), feed, RuntimeState.empty(params(), 100.0),
        )
    monkeypatch.setattr(repositories, "insert", original_insert)

    assert feed.next_calls == 1 and feed.closed
    assert database.connection.execute("SELECT COUNT(*) FROM processed_1m_bar").fetchone()[0] == 0
    assert database.connection.execute("SELECT status FROM single_day_run").fetchone()[0] == "FAILED"


def test_completion_terminal_failure_becomes_failed_terminal_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 33, tzinfo=ET))
    database = Database(tmp_path / "phase5-terminal.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    feed = Feed([
        FeedEvent(FeedStatus.BAR_AVAILABLE, bar(0, BarSource.END)),
        FeedEvent(FeedStatus.BAR_END, None),
    ])

    def fail_complete(_: object) -> None:
        raise PersistenceError("completed summary write failed")

    monkeypatch.setattr(repositories, "complete_with_summary", fail_complete)
    with pytest.raises(PersistenceError, match="completed summary write failed"):
        SingleDayRunner(database, repositories, clock).execute_run(
            context(clock), feed, RuntimeState.empty(params(), 100.0),
        )

    assert database.connection.execute("SELECT status FROM single_day_run").fetchone()[0] == "FAILED"
    assert database.connection.execute("SELECT status FROM run_summary").fetchone()[0] == "FAILED"
