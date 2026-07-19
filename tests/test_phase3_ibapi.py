from __future__ import annotations

import csv
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from single_day_test.bar_feed.bar_validation import validate_complete_backtest_day
from single_day_test.domain.errors import HistoricalDataError, PersistenceError
from single_day_test.domain.enums import BarSource, DecisionLabel, Direction, RunMode, RunStatus, ThresholdMode, TrendLabel
from single_day_test.domain.models import ChannelResult, DecisionResult, ProcessedBarRecord, RawBar, RunContext, RunSummary, SignalEvent, TradingSession, TrendResult
from single_day_test.domain.parameters import ParameterSet
from single_day_test.ib.config import IbConfig
from single_day_test.ib.gateway import IbApiGateway, LiveBarCallbacks, _PendingRequest
from single_day_test.ib.services import HistoricalBarService, TradingSessionService
from single_day_test.persistence.database import SCHEMA_VERSION, Database, SqliteRepositories

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


class EventLogger:
    trace_enabled = True
    def __init__(self) -> None: self.events: list[tuple[str, str, dict[str, object]]] = []
    def info(self, event: str, **fields: object) -> None: self.events.append(("INFO", event, fields))
    def error(self, event: str, **fields: object) -> None: self.events.append(("ERROR", event, fields))
    def summary(self, event: str, **fields: object) -> None: self.events.append(("INFO", event, fields))
    def stop_info_trace(self) -> None: self.trace_enabled = False


def test_callback_error_is_logged_with_full_ibapi_context() -> None:
    logger = EventLogger()
    api = IbApiGateway(IbConfig("127.0.0.1", 7497, 71, 0.1), logger)

    api.error(9, 123, 2104, "Market data farm connection is OK", "")

    level, event, fields = logger.events[-1]
    assert level == "ERROR" and event == "ibapi_error_callback"
    assert fields == {
        "request_id": 9, "error_time": 123, "error_code": 2104,
        "error_message": "Market data farm connection is OK", "advanced_order_reject_json": None,
    }


def test_callback_error_fails_matching_live_subscription() -> None:
    api = IbApiGateway(IbConfig("127.0.0.1", 7497, 71, 0.1))
    errors: list[Exception] = []
    api._live_callbacks[8] = ("AAPL", LiveBarCallbacks(lambda _: None, lambda: None, lambda _: None, errors.append))

    api.error(8, 0, 162, "No market data")

    assert len(errors) == 1
    assert "IBAPI error 162 for request 8" in str(errors[0])


def test_system_connection_error_keeps_live_subscription_active() -> None:
    api = IbApiGateway(IbConfig("127.0.0.1", 7497, 71, 0.1))
    errors: list[Exception] = []
    api._live_callbacks[8] = ("AAPL", LiveBarCallbacks(lambda _: None, lambda: None, lambda _: None, errors.append))

    api.error(-1, 0, 1100, "Connectivity between IBKR and Trader Workstation has been lost.")

    assert errors == [] and 8 in api._live_callbacks


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
    timestamp = repos.database.connection.execute("SELECT timestamp FROM raw_1m_bar LIMIT 1").fetchone()[0]
    assert timestamp == "2025-01-15T09:30:00-05:00"
    HistoricalBarService(repos, fake).load_or_fetch("AAPL", session)
    assert fake.bar_calls == 1


def test_invalid_ibapi_day_is_not_persisted(tmp_path) -> None:
    database = Database(tmp_path / "phase3.sqlite3"); database.initialize(); repos = SqliteRepositories(database)
    fake = FakeGateway(_session(), _bars()[:-1])
    with pytest.raises(HistoricalDataError):
        HistoricalBarService(repos, fake).load_or_fetch("AAPL", _session())
    assert repos.load_rth_bars("AAPL", _session().trade_date) == []


def test_nonconforming_schema_is_not_cleared(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    sqlite3.connect(path).execute("CREATE TABLE raw_1m_bar (timestamp TEXT)").connection.commit()
    database = Database(path); database.initialize()
    assert database.connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == SCHEMA_VERSION
    assert [row[1] for row in database.connection.execute("PRAGMA table_info(raw_1m_bar)")] == ["timestamp"]


def test_previous_schema_version_is_advanced_without_rebuild(tmp_path) -> None:
    path = tmp_path / "previous_phase3.sqlite3"
    database = Database(path)
    database.initialize()
    database.connection.execute("INSERT INTO raw_1m_bar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                ("AAPL", 1736951400, "2025-01-15T09:30:00-05:00", 1, 2, 0, 1, 10, 1, 1, "1 min", "TRADES", 1, "test", 1, 1,))
    database.connection.commit()
    database.connection.execute("UPDATE schema_meta SET value='phase3_ibapi_v4' WHERE key='schema_version'")
    database.connection.commit()
    database.initialize()
    assert database.connection.execute("SELECT COUNT(*) FROM raw_1m_bar").fetchone()[0] == 1
    assert database.connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == SCHEMA_VERSION
    columns = {row[1] for row in database.connection.execute("PRAGMA table_info(processed_1m_bar)")}
    assert {"date", "timestamp", "wap", "bar_count", "bar_size", "what_to_show", "use_rth", "source", "trend_slope", "channel_window", "channel_pred_high", "decision_triggered", "curr_mix_ratio", "channel_last_pred_high", "channel_last_pred_low", "channel_curr_pred_high", "channel_curr_pred_low", "channel_mix"} <= columns
    assert not {"slope_std_window", "dev_window", "residual_window"} & columns
    assert not any(column.lower().endswith("_et") or column.lower() == "et" for column in columns)
    assert "initial_threshold" not in columns
    assert not {"parameter_snapshot_json", "trend_json", "channel_json", "decision_json"} & columns


def test_dual_reward_schema_upgrade_preserves_legacy_metrics_without_backfill(tmp_path) -> None:
    database = Database(tmp_path / "metrics-upgrade.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    params = ParameterSet("p1", 3, 3, 0.8, 95.0, 95.0, 1, 1)
    started = datetime(2025, 1, 15, 9, 0, tzinfo=ET)
    context = RunContext("run-upgrade", "AAPL", date(2025, 1, 15), params, Direction.BUY, ThresholdMode.FIXED, 100.0, RunMode.BACKTEST, None, started)
    repositories.create(context)
    database.connection.execute(
        "UPDATE single_day_run SET status='COMPLETED', ended_at_epoch=?, "
        "processed_bar_count=2, signal_count=2, best_reward=?, efficiency=? "
        "WHERE run_id='run-upgrade'",
        (int(datetime(2025, 1, 15, 16, 0, tzinfo=ET).timestamp()), 0.9, 0.81),
    )
    database.connection.execute(
        '''INSERT INTO run_summary (
             run_id, status, processed_bar_count, signal_count,
             avg_best_reward_per_day, avg_efficiency_per_day,
             started_at_epoch, ended_at_epoch
           ) VALUES ('run-upgrade', 'COMPLETED', 2, 2, 0.9, 0.81, ?, ?)''',
        (int(started.timestamp()), int(datetime(2025, 1, 15, 16, 0, tzinfo=ET).timestamp())),
    )
    database.connection.execute(
        "UPDATE schema_meta SET value='reward_efficiency_v1' WHERE key='schema_version'"
    )
    database.connection.commit()

    database.initialize()

    daily = database.connection.execute(
        "SELECT best_reward, efficiency, first_trigger_reward, full_position_reward "
        "FROM single_day_run WHERE run_id='run-upgrade'"
    ).fetchone()
    aggregate = database.connection.execute(
        "SELECT avg_best_reward_per_day, avg_efficiency_per_day, "
        "avg_first_trigger_reward_per_day, avg_full_position_reward_per_day FROM run_summary "
        "WHERE run_id='run-upgrade'"
    ).fetchone()
    assert tuple(daily) == (0.9, 0.81, None, None)
    assert tuple(aggregate) == (0.9, 0.81, None, None)


def test_run_summary_upgrade_appends_metric_day_columns_without_rewriting_rows(tmp_path) -> None:
    path = tmp_path / "summary-columns-upgrade.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta VALUES ('schema_version', 'channel_mix_v1');"
        "CREATE TABLE run_summary ("
        "run_id TEXT PRIMARY KEY, status TEXT NOT NULL, processed_bar_count INTEGER NOT NULL, "
        "signal_count INTEGER NOT NULL, avg_signal_count_per_day REAL, "
        "avg_best_reward_per_day REAL, avg_efficiency_per_day REAL, "
        "max_signal_count_per_day INTEGER, max_best_reward_per_day REAL, "
        "max_efficiency_per_day REAL, started_at_epoch INTEGER NOT NULL, "
        "ended_at_epoch INTEGER NOT NULL, error_type TEXT, error_message TEXT)"
    )
    connection.execute(
        "INSERT INTO run_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy", "COMPLETED", 10, 1, 1.0, 0.9, 0.9, 1, 0.9, 0.9, 1, 2, None, None),
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()

    row = database.connection.execute(
        "SELECT avg_best_reward_per_day, avg_efficiency_per_day, "
        "max_best_reward_days, max_efficiency_days, "
        "avg_first_trigger_reward_per_day, avg_full_position_reward_per_day "
        "FROM run_summary "
        "WHERE run_id='legacy'"
    ).fetchone()
    assert tuple(row) == (0.9, 0.9, None, None, None, None)


def test_initialize_adds_channel_mix_columns_to_an_old_processed_table(tmp_path) -> None:
    path = tmp_path / "legacy_processed.sqlite3"
    database = Database(path)
    database.initialize()
    old_columns = [
        row[1]
        for row in database.connection.execute("PRAGMA table_info(processed_1m_bar)")
        if row[1]
        not in {
            "curr_mix_ratio", "channel_last_pred_high", "channel_last_pred_low",
            "channel_curr_pred_high", "channel_curr_pred_low", "channel_mix",
        }
    ]
    database.connection.execute(
        f"CREATE TABLE processed_legacy AS SELECT {', '.join(old_columns)} FROM processed_1m_bar WHERE 0"
    )
    database.connection.execute("DROP TABLE processed_1m_bar")
    database.connection.execute("ALTER TABLE processed_legacy RENAME TO processed_1m_bar")
    database.connection.commit()

    database.initialize()

    columns = {row[1] for row in database.connection.execute("PRAGMA table_info(processed_1m_bar)")}
    assert {
        "curr_mix_ratio", "channel_last_pred_high", "channel_last_pred_low",
        "channel_curr_pred_high", "channel_curr_pred_low", "channel_mix",
    } <= columns


def test_processed_run_csv_uses_the_processed_table_fields(tmp_path) -> None:
    database = Database(tmp_path / "phase3.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    timestamp = datetime(2025, 1, 15, 10, 0, tzinfo=ET)
    record = ProcessedBarRecord(
        "run-1", "AAPL", date(2025, 1, 15), timestamp, RunMode.BACKTEST, BarSource.HIST,
        Direction.BUY, "params", {"trend_window": 30, "channel_window": 5,
        "r2_threshold": 0.5, "channel_high_percentile": 95.0,
        "channel_low_percentile": 5.0, "continuous_break_count": 3,
        "curr_mix_ratio": 0.25, "is_active": 1}, 0.0,
        100.0, 101.0, 99.0, 100.5, 10.0, 100.25, 3,
        TrendResult(100.5, 0.1, 0.9, 0.01, 0.02, True, TrendLabel.UP, 30),
        ChannelResult(
            None, None, TrendLabel.UP, None, None, None, None, None,
            0.1, 100.0, 95.0, 5.0, 30,
            last_pred_high=110.0, last_pred_low=90.0,
            curr_pred_high=108.0, curr_pred_low=92.0, mix=0.25,
        ),
        DecisionResult(DecisionLabel.BUY, 3, True),
    )
    records = [record, replace(
        record,
        timestamp_et=timestamp + timedelta(minutes=1),
        decision=DecisionResult(DecisionLabel.NO_BUY, 0, False),
    )]
    path = repositories.export_processed_run_csv("run-1", records, tmp_path / "data")
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    columns = [column[1] for column in database.connection.execute("PRAGMA table_info(processed_1m_bar)")]
    assert path.name == "run-1.csv"
    assert list(rows[0]) == columns
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["timestamp"] == "2025-01-15T10:00:00-05:00"
    assert rows[0]["curr_mix_ratio"] == "0.25"
    assert rows[0]["channel_last_pred_high"] == "110.0"
    assert rows[0]["channel_curr_pred_high"] == "108.0"
    assert rows[0]["channel_mix"] == "0.25"
    assert [row["decision"] for row in rows] == ["BUY", ""]
    assert database.connection.execute("SELECT COUNT(*) FROM processed_1m_bar WHERE run_id='run-1'").fetchone()[0] == 0


def test_terminal_daily_statistics_and_scan_summary_use_persisted_prices(tmp_path) -> None:
    database = Database(tmp_path / "statistics.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    params = ParameterSet("p1", 3, 3, 0.8, 95.0, 95.0, 1, 1)
    started = datetime(2025, 1, 15, 9, 0, tzinfo=ET)
    context = RunContext("run-1", "AAPL", date(2025, 1, 15), params, Direction.BUY, ThresholdMode.FIXED, 100.0, RunMode.BACKTEST, None, started)
    repositories.create(context)
    base = ProcessedBarRecord(
        "run-1", "AAPL", context.trade_date, datetime(2025, 1, 15, 9, 30, tzinfo=ET), RunMode.BACKTEST, BarSource.HIST,
        Direction.BUY, "p1", {"trend_window": 3, "channel_window": 3,
        "r2_threshold": 0.8, "channel_high_percentile": 95.0, "channel_low_percentile": 95.0, "continuous_break_count": 1}, 100.0,
        100.0, 101.0, 99.0, 100.0, 1.0, 100.0, 1,
        TrendResult(100.0, None, None, None, None, None, None, 1),
        ChannelResult(None, None, None, None, None, None, None, None, None, None, None, None, 1),
        DecisionResult(DecisionLabel.NO_BUY, 0, False),
    )
    second = replace(base, timestamp_et=datetime(2025, 1, 15, 9, 31, tzinfo=ET), trend=TrendResult(90.0, None, None, None, None, None, None, 1), decision=DecisionResult(DecisionLabel.BUY, 1, True))
    summary = RunSummary("run-1", "AAPL", context.trade_date, RunMode.BACKTEST, Direction.BUY, "p1", {}, 2, 1, RunStatus.COMPLETED, started, datetime(2025, 1, 15, 16, 0, tzinfo=ET), None, None, 100.0, 90.0, 1.0, 0.5)
    repositories.complete_with_summary(summary)
    daily = database.connection.execute("SELECT first_threshold, processed_bar_count, signal_count, best_price, first_trigger_reward, full_position_reward, best_order_price, best_reward, efficiency FROM single_day_run").fetchone()
    assert tuple(daily[:6]) == pytest.approx((100.0, 2, 1, 90.0, 1.0, 0.5))
    assert tuple(daily[6:]) == (None, None, None)
    aggregate = database.connection.execute("SELECT run_id, processed_bar_count, signal_count, avg_signal_count_per_day, avg_first_trigger_reward_per_day, avg_full_position_reward_per_day, max_signal_count_per_day, max_first_trigger_reward_per_day, max_full_position_reward_per_day, max_first_trigger_reward_days, max_full_position_reward_days FROM run_summary").fetchone()
    assert aggregate["run_id"] == "run-1"
    assert tuple(aggregate)[1:9] == pytest.approx((2, 1, 1.0, 1.0, 0.5, 1, 1.0, 0.5))
    assert aggregate["max_first_trigger_reward_days"] == "2025-01-15"
    assert aggregate["max_full_position_reward_days"] == "2025-01-15"

    zero_context = RunContext("run-zero", "AAPL", context.trade_date, params, Direction.SELL, ThresholdMode.FIXED, 0.0, RunMode.BACKTEST, None, started)
    repositories.create(zero_context)
    zero_record = replace(base, run_id="run-zero", direction=Direction.SELL, active_threshold=0.0)
    with database.transaction():
        repositories.insert(zero_record)
    zero_summary = RunSummary("run-zero", "AAPL", zero_context.trade_date, RunMode.BACKTEST, Direction.SELL, "p1", {}, 1, 0, RunStatus.COMPLETED, started, datetime(2025, 1, 15, 16, 0, tzinfo=ET), None, None, None, None, 0.0, 0.0)
    repositories.complete_with_summary(zero_summary)
    zero_daily = database.connection.execute("SELECT signal_count, best_price, first_trigger_reward, full_position_reward FROM single_day_run WHERE run_id='run-zero'").fetchone()
    assert tuple(zero_daily) == (0, None, 0.0, 0.0)


def test_run_summary_lists_all_tied_maximum_metric_days(tmp_path) -> None:
    database = Database(tmp_path / "metric-days.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    params = ParameterSet("p1", 3, 3, 0.8, 95.0, 95.0, 1, 1)
    started = datetime(2025, 1, 2, 9, 0, tzinfo=ET)

    for trade_date, first_reward, full_reward in (
        (date(2025, 1, 2), 0.8, 0.64),
        (date(2025, 1, 3), 0.9, 0.81),
        (date(2025, 1, 6), 0.9, 0.81),
    ):
        context = RunContext("run-many", "AAPL", trade_date, params, Direction.BUY, ThresholdMode.FIXED, 100.0, RunMode.BACKTEST, None, started)
        repositories.create(context)
        repositories.complete_with_summary(
            RunSummary(
                "run-many", "AAPL", trade_date, RunMode.BACKTEST, Direction.BUY,
                "p1", {}, 1, 1, RunStatus.COMPLETED, started,
                datetime(2025, 1, 2, 16, 0, tzinfo=ET), None, None,
                100.0, 90.0, first_reward, full_reward,
            ),
            write_run_summary=False,
        )

    repositories.save_run_summary("run-many")
    summary = database.connection.execute(
        "SELECT avg_first_trigger_reward_per_day, avg_full_position_reward_per_day, "
        "max_first_trigger_reward_per_day, max_full_position_reward_per_day, "
        "max_first_trigger_reward_days, max_full_position_reward_days "
        "FROM run_summary WHERE run_id='run-many'"
    ).fetchone()

    assert tuple(summary[:4]) == pytest.approx(((0.8 + 0.9 + 0.9) / 3, (0.64 + 0.81 + 0.81) / 3, 0.9, 0.81))
    assert summary["max_first_trigger_reward_days"] == "2025-01-03,2025-01-06"
    assert summary["max_full_position_reward_days"] == "2025-01-03,2025-01-06"
