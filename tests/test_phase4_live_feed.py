from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from threading import Thread
from zoneinfo import ZoneInfo

import pytest

from single_day_test.bar_feed.live_paper_feed import LivePaperFeed
from single_day_test.domain.enums import BarSource, FeedStatus
from single_day_test.domain.errors import BarOrderingError
from single_day_test.domain.models import RawBar, TradingSession
from single_day_test.ib.gateway import LiveBarCallbacks
from single_day_test.persistence.database import Database, SqliteRepositories

ET = ZoneInfo("America/New_York")


@dataclass
class Clock:
    value: datetime
    def now_et(self) -> datetime: return self.value


class Handle:
    def __init__(self) -> None: self.closed = False
    def close(self) -> None: self.closed = True


class Gateway:
    def __init__(self) -> None: self.callbacks: LiveBarCallbacks | None = None; self.handle = Handle(); self.duration = 0
    def start_live_1m_bars(self, symbol: str, duration_seconds: int, callbacks: LiveBarCallbacks) -> Handle:
        self.duration, self.callbacks = duration_seconds, callbacks
        return self.handle


def bar(index: int, volume: float = 1) -> RawBar:
    stamp = datetime(2025, 1, 2, 9, 30, tzinfo=ET) + timedelta(minutes=index)
    return RawBar("AAPL", int(stamp.timestamp()), 100, 101, 99, 100, volume, 100, 1)


def test_live_feed_merges_history_then_emits_live_and_end(tmp_path) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 32, 10, tzinfo=ET))
    session = TradingSession(date(2025, 1, 2), True, datetime(2025, 1, 2, 9, 30, tzinfo=ET), datetime(2025, 1, 2, 9, 33, tzinfo=ET))
    database = Database(tmp_path / "live.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    gateway = Gateway(); feed = LivePaperFeed("AAPL", session, gateway, repos, clock)
    feed.start(); assert gateway.duration == 140
    assert gateway.callbacks is not None
    gateway.callbacks.historical(bar(0)); gateway.callbacks.historical_end()
    first = feed.next_event(); assert first.status is FeedStatus.BAR_AVAILABLE and first.bar is not None and first.bar.source is BarSource.HIST
    gateway.callbacks.update(bar(1)); gateway.callbacks.update(bar(2))
    second = feed.next_event(); assert second.bar is not None and second.bar.source is BarSource.LIVE
    clock.value = datetime(2025, 1, 2, 9, 33, tzinfo=ET)
    final = feed.next_event(); assert final.bar is not None and final.bar.source is BarSource.END
    assert feed.next_event().status is FeedStatus.BAR_END
    assert database.connection.execute("SELECT COUNT(*) FROM raw_1m_bar").fetchone()[0] == 3
    feed.close(); assert gateway.handle.closed


def test_live_feed_uses_ibkr_minimum_window_at_session_start(tmp_path) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 30, tzinfo=ET))
    session = TradingSession(date(2025, 1, 2), True, datetime(2025, 1, 2, 9, 30, tzinfo=ET), datetime(2025, 1, 2, 16, 0, tzinfo=ET))
    database = Database(tmp_path / "minimum-window.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    gateway = Gateway(); feed = LivePaperFeed("AAPL", session, gateway, repos, clock)

    feed.start()

    assert gateway.duration == 60


def test_live_feed_ignores_only_first_pre_session_historical_boundary_bar(tmp_path) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 32, 10, tzinfo=ET))
    session = TradingSession(date(2025, 1, 2), True, datetime(2025, 1, 2, 9, 30, tzinfo=ET), datetime(2025, 1, 2, 9, 35, tzinfo=ET))
    database = Database(tmp_path / "boundary.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    gateway = Gateway(); feed = LivePaperFeed("AAPL", session, gateway, repos, clock)
    feed.start(); assert gateway.callbacks is not None
    prior_session_bar = RawBar("AAPL", int(datetime(2025, 1, 1, 15, 59, tzinfo=ET).timestamp()), 100, 101, 99, 100, 1, 100, 1)
    gateway.callbacks.historical(prior_session_bar)
    gateway.callbacks.historical(bar(0)); gateway.callbacks.historical_end()
    event = feed.next_event()
    assert event.status is FeedStatus.BAR_AVAILABLE and event.bar is not None
    assert event.bar.raw.timestamp_et == bar(0).timestamp_et
    assert database.connection.execute("SELECT COUNT(*) FROM raw_1m_bar").fetchone()[0] == 1


def test_live_feed_persists_initial_history_from_ibapi_callback_thread(tmp_path) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 32, 10, tzinfo=ET))
    session = TradingSession(date(2025, 1, 2), True, datetime(2025, 1, 2, 9, 30, tzinfo=ET), datetime(2025, 1, 2, 9, 35, tzinfo=ET))
    database = Database(tmp_path / "callback-thread.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    gateway = Gateway(); feed = LivePaperFeed("AAPL", session, gateway, repos, clock)
    feed.start(); assert gateway.callbacks is not None
    worker = Thread(target=lambda: (gateway.callbacks.historical(bar(0)), gateway.callbacks.historical_end()))
    worker.start(); worker.join()
    event = feed.next_event()
    assert event.status is FeedStatus.BAR_AVAILABLE and event.bar is not None
    assert database.connection.execute("SELECT COUNT(*) FROM raw_1m_bar").fetchone()[0] == 1


def test_late_completed_bar_raises(tmp_path) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 32, tzinfo=ET))
    session = TradingSession(date(2025, 1, 2), True, datetime(2025, 1, 2, 9, 30, tzinfo=ET), datetime(2025, 1, 2, 9, 35, tzinfo=ET))
    database = Database(tmp_path / "late.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    feed = LivePaperFeed("AAPL", session, Gateway(), repos, clock)
    feed._process_batch([bar(1)])
    with pytest.raises(BarOrderingError): feed._process_batch([bar(0)])


def test_live_feed_uses_session_deadlines_when_runner_waits_without_timeout(tmp_path) -> None:
    clock = Clock(datetime(2025, 1, 2, 9, 32, tzinfo=ET))
    session = TradingSession(date(2025, 1, 2), True, datetime(2025, 1, 2, 9, 30, tzinfo=ET), datetime(2025, 1, 2, 9, 35, tzinfo=ET))
    database = Database(tmp_path / "deadline.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    feed = LivePaperFeed("AAPL", session, Gateway(), repos, clock)

    assert feed._wait_timeout(None) == 180.0
    clock.value = datetime(2025, 1, 2, 9, 35, tzinfo=ET)
    assert feed._wait_timeout(None) == 60.0
    assert feed._wait_timeout(2.5) == 2.5
