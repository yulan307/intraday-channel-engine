from __future__ import annotations

import csv
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from single_day_test.bar_feed.bar_validation import validate_complete_backtest_day
from single_day_test.domain.errors import HistoricalDataError, PersistenceError
from single_day_test.domain.enums import BarSource, DecisionLabel, Direction, RunMode, TrendLabel
from single_day_test.domain.models import ChannelResult, DecisionResult, ProcessedBarRecord, RawBar, TradingSession, TrendResult
from single_day_test.ib.config import IbConfig
from single_day_test.ib.gateway import IbApiGateway, _PendingRequest
from single_day_test.ib.services import HistoricalBarService, TradingSessionService
from single_day_test.persistence.database import Database, SqliteRepositories

ET = ZoneInfo("America/New_York")


def _session() -> TradingSession:
    day = date(2025, 1, 15)
    return TradingSession(day, True, datetime(2025, 1, 15, 9, 30, tzinfo=ET), datetime(2025, 1, 15, 16, 0, tzinfo=ET))


def _bars(symbol: str = "AAPL") -> list[RawBar]:
    start = _session().session_start_et
    assert start is not None
    return [RawBar(symbol, int((start + timedelta(minutes=i)).timestamp()), 100, 101, 99, 100.5, 10, 100.25, 3) for i in range(390)]


class FakeGateway:
    def __init__(self, session: TradingSession, bars: list[RawBar]) -> None:
        self.session, self.bars, self.session_calls, self.bar_calls = session, bars, 0, 0
    def query_trading_session(self, symbol: str, trade_date: date) -> TradingSession:
        self.session_calls += 1; return self.session
    def request_historical_1m_bars(self, symbol: str, start_et: datetime, end_et: datetime) -> list[RawBar]:
        self.bar_calls += 1; return self.bars


def test_ibapi_bar_mapping_uses_epoch_and_native_fields() -> None:
    api = IbApiGateway(IbConfig("127.0.0.1", 7497, 71, 0.1))
    mapped = api._raw_bar("AAPL", SimpleNamespace(date="1736951400", open=100, high=101, low=99, close=100.5, volume=22, wap=100.2, barCount=7))
    assert mapped.date == 1736951400
    assert mapped.wap == 100.2 and mapped.barCount == 7
    assert mapped.timestamp_et.tzinfo == ET


def test_callback_bridge_correlates_and_completes_request() -> None:
    api = IbApiGateway(IbConfig("127.0.0.1", 7497, 71, 0.1))
    pending = _PendingRequest(symbol="AAPL")
    api._pending[42] = pending
    api.historicalData(42, SimpleNamespace(date="1736951400", open=100, high=101, low=99, close=100.5, volume=22, wap=100.2, barCount=7))
    api.historicalDataEnd(42, "", "")
    assert pending.event.is_set() and pending.bars[0].symbol == "AAPL"


def test_callback_error_completes_matching_request() -> None:
    api = IbApiGateway(IbConfig("127.0.0.1", 7497, 71, 0.1))
    pending = _PendingRequest(); api._pending[9] = pending
    api.error(9, 0, 162, "No market data")
    assert pending.event.is_set() and pending.error is not None


def test_connection_close_fails_all_pending_requests() -> None:
    api = IbApiGateway(IbConfig("127.0.0.1", 7497, 71, 0.1))
    pending = _PendingRequest(); api._pending[10] = pending
    api.connectionClosed()
    assert pending.event.is_set() and pending.error is not None


def test_services_cache_schedule_and_validated_ibapi_bars(tmp_path) -> None:
    database = Database(tmp_path / "phase3.sqlite3")
    database.initialize(); repos = SqliteRepositories(database)
    session = _session(); fake = FakeGateway(session, _bars())
    resolved = TradingSessionService(repos, fake).resolve("AAPL", session.trade_date)
    assert resolved == session and fake.session_calls == 1
    result = HistoricalBarService(repos, fake).load_or_fetch("AAPL", session)
    assert len(result) == 390 and fake.bar_calls == 1
    assert validate_complete_backtest_day(repos.load_rth_bars("AAPL", session.trade_date), session)
    HistoricalBarService(repos, fake).load_or_fetch("AAPL", session)
    assert fake.bar_calls == 1


def test_invalid_ibapi_day_is_not_persisted(tmp_path) -> None:
    database = Database(tmp_path / "phase3.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    fake = FakeGateway(_session(), _bars()[:-1])
    with pytest.raises(HistoricalDataError):
        HistoricalBarService(repos, fake).load_or_fetch("AAPL", _session())
    assert repos.load_rth_bars("AAPL", _session().trade_date) == []


def test_nonconforming_schema_is_cleared_once(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    sqlite3.connect(path).execute("CREATE TABLE raw_1m_bar (timestamp TEXT)").connection.commit()
    database = Database(path); database.initialize()
    assert database.connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "phase3_expand_v1"


def test_previous_phase3_schema_is_cleared_once_and_recreated(tmp_path) -> None:
    path = tmp_path / "previous_phase3.sqlite3"
    database = Database(path)
    database.initialize()
    database.connection.execute("INSERT INTO raw_1m_bar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                ("AAPL", 1736951400, 1, 2, 0, 1, 10, 1, 1, "1 min", "TRADES", 1, "test", 1, 1,))
    database.connection.commit()
    database.connection.execute("UPDATE schema_meta SET value='phase3_ibapi_v4' WHERE key='schema_version'")
    database.connection.commit()
    database.initialize()
    assert database.connection.execute("SELECT COUNT(*) FROM raw_1m_bar").fetchone()[0] == 0
    assert database.connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "phase3_expand_v1"
    columns = {row[1] for row in database.connection.execute("PRAGMA table_info(processed_1m_bar)")}
    assert {"date", "timestamp", "wap", "bar_count", "bar_size", "what_to_show", "use_rth", "source", "trend_slope", "channel_pred_high", "decision_triggered"} <= columns
    assert not any(column.lower().endswith("_et") or column.lower() == "et" for column in columns)
    assert "initial_threshold" not in columns
    assert not {"parameter_snapshot_json", "trend_json", "channel_json", "decision_json"} & columns


def test_processed_run_csv_uses_the_processed_table_fields(tmp_path) -> None:
    database = Database(tmp_path / "phase3.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    timestamp = datetime(2025, 1, 15, 10, 0, tzinfo=ET)
    record = ProcessedBarRecord(
        "run-1", "AAPL", date(2025, 1, 15), timestamp, RunMode.BACKTEST, BarSource.HIST,
        Direction.BUY, "params", {"trend_window": 30, "slope_std_window": 5, "dev_window": 5,
        "residual_window": 5, "r2_threshold": 0.5, "channel_high_percentile": 95.0,
        "channel_low_percentile": 5.0, "continuous_break_count": 3, "is_active": 1}, 0.0,
        100.0, 101.0, 99.0, 100.5, 10.0, 100.25, 3,
        TrendResult(100.5, 0.1, 0.9, 0.01, 0.02, True, TrendLabel.UP, 30),
        ChannelResult(None, None, TrendLabel.UP, None, None, None, None, None, 0.1, 100.0, 95.0, 5.0, 30),
        DecisionResult(DecisionLabel.BUY, 3, True),
    )
    with database.transaction():
        repositories.insert(record)
    path = repositories.export_processed_run_csv("run-1", tmp_path / "data")
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    columns = [column[1] for column in database.connection.execute("PRAGMA table_info(processed_1m_bar)")]
    assert path.name == "run-1.csv"
    assert list(rows[0]) == columns
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["timestamp"] == "2025-01-15T10:00:00-05:00"
