from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from single_day_test.bar_feed.bar_validation import validate_complete_backtest_day
from single_day_test.domain.errors import HistoricalDataError, PersistenceError
from single_day_test.domain.models import RawBar, TradingSession
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


def test_legacy_schema_requires_explicit_one_time_rebuild(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    sqlite3.connect(path).execute("CREATE TABLE raw_1m_bar (timestamp TEXT)").connection.commit()
    with pytest.raises(PersistenceError, match="Legacy Phase 2"):
        Database(path).initialize()
    database = Database(path, rebuild_legacy=True); database.initialize()
    assert database.connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "phase3_ibapi_v1"
