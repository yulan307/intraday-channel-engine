from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..domain.enums import RunStatus
from ..domain.errors import PersistenceError
from ..domain.models import ProcessedBarRecord, RawBar, RunContext, RunSummary, SignalEvent, TradingSession

SCHEMA_VERSION = "phase3_expand_v3"


class Database:
    """SQLite storage with a deliberately non-compatible Phase 3 schema."""
    def __init__(self, path: str | Path, *, rebuild_legacy: bool = False) -> None:
        self.path = str(path)
        # IBAPI invokes live historical callbacks on its event-loop thread.
        # LivePaperFeed serializes its callback persistence with its condition
        # lock, so the connection must permit that owning callback thread.
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.rebuild_legacy = rebuild_legacy

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise PersistenceError(str(exc)) from exc

    def initialize(self) -> None:
        marker = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
        if marker is not None:
            version = self.connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if version is not None and version[0] == SCHEMA_VERSION and self._schema_is_current():
                return
            self._drop_all()
        elif self.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_1m_bar'").fetchone() is not None:
            self._drop_all()
        self.connection.executescript('''
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE trade_date (
          trade_date TEXT PRIMARY KEY, is_trading_day INTEGER NOT NULL,
          session_start_epoch INTEGER, session_end_epoch INTEGER,
          source TEXT NOT NULL, created_at_epoch INTEGER NOT NULL, updated_at_epoch INTEGER NOT NULL);
        CREATE TABLE raw_1m_bar (
          symbol TEXT NOT NULL, date INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
          low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, wap REAL NOT NULL,
          bar_count INTEGER NOT NULL, bar_size TEXT NOT NULL, what_to_show TEXT NOT NULL,
          use_rth INTEGER NOT NULL, source TEXT NOT NULL, created_at_epoch INTEGER NOT NULL,
          updated_at_epoch INTEGER NOT NULL, PRIMARY KEY(symbol, date));
        CREATE TABLE single_day_run (
          run_id TEXT NOT NULL, trade_date TEXT NOT NULL, symbol TEXT NOT NULL, mode TEXT NOT NULL,
          live_phase TEXT, direction TEXT NOT NULL, parameter_set_id TEXT NOT NULL,
          parameter_snapshot_json TEXT NOT NULL, threshold_mode TEXT NOT NULL,
          fixed_threshold REAL, threshold_update_rate REAL NOT NULL, status TEXT NOT NULL, started_at_epoch INTEGER NOT NULL,
          ended_at_epoch INTEGER, error_type TEXT, error_message TEXT,
          PRIMARY KEY(run_id, trade_date));
        CREATE TABLE processed_1m_bar (
          run_id TEXT NOT NULL, date INTEGER NOT NULL, timestamp TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
          mode TEXT NOT NULL, bar_source TEXT NOT NULL, direction TEXT NOT NULL, parameter_set_id TEXT NOT NULL,
          trend_window INTEGER NOT NULL, slope_std_window INTEGER NOT NULL, dev_window INTEGER NOT NULL,
          residual_window INTEGER NOT NULL, r2_threshold REAL NOT NULL,
          channel_high_percentile REAL NOT NULL, channel_low_percentile REAL NOT NULL,
          continuous_break_count INTEGER NOT NULL,
          active_threshold REAL,
          open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
          volume REAL NOT NULL, wap REAL NOT NULL, bar_count INTEGER NOT NULL,
          bar_size TEXT NOT NULL, what_to_show TEXT NOT NULL, use_rth INTEGER NOT NULL,
          source TEXT NOT NULL,
          trend_price REAL NOT NULL, trend_slope REAL, trend_r2 REAL, trend_slope_rmse REAL,
          trend_slope_std REAL, trend_fit_ok INTEGER, trend_raw_trend TEXT,
          trend_stack_length_after INTEGER NOT NULL,
          channel_pred_high REAL, channel_pred_low REAL, channel_effective_trend TEXT,
          channel_last_trend_slope REAL, channel_last_trend_intercept REAL,
          channel_last_trend_bar_count INTEGER, channel_last_high_percentile REAL,
          channel_last_low_percentile REAL, channel_curr_trend_slope REAL,
          channel_curr_trend_intercept REAL, channel_curr_high_percentile REAL,
          channel_curr_low_percentile REAL, channel_stack_length_after INTEGER NOT NULL,
          decision TEXT, decision_recorded_break_count INTEGER NOT NULL,
          decision_triggered INTEGER NOT NULL,
          PRIMARY KEY(run_id, date));
        CREATE TABLE signal_event (
          run_id TEXT NOT NULL, date INTEGER NOT NULL, decision TEXT NOT NULL, price REAL NOT NULL,
          break_count INTEGER NOT NULL, PRIMARY KEY(run_id, date));
        CREATE TABLE run_summary (
          run_id TEXT NOT NULL, trade_date TEXT NOT NULL, status TEXT NOT NULL, processed_bar_count INTEGER NOT NULL,
          signal_count INTEGER NOT NULL, final_curr_slope REAL, final_curr_intercept REAL,
          final_high_percentile REAL, final_low_percentile REAL, final_channel_length INTEGER NOT NULL,
          started_at_epoch INTEGER NOT NULL, ended_at_epoch INTEGER NOT NULL, error_type TEXT, error_message TEXT,
          PRIMARY KEY(run_id, trade_date));
        ''')
        self.connection.execute("INSERT INTO schema_meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        self.connection.commit()

    def _schema_is_current(self) -> bool:
        expected = {
            "schema_meta": ["key", "value"],
            "trade_date": ["trade_date", "is_trading_day", "session_start_epoch", "session_end_epoch", "source", "created_at_epoch", "updated_at_epoch"],
            "raw_1m_bar": ["symbol", "date", "open", "high", "low", "close", "volume", "wap", "bar_count", "bar_size", "what_to_show", "use_rth", "source", "created_at_epoch", "updated_at_epoch"],
            "single_day_run": ["run_id", "trade_date", "symbol", "mode", "live_phase", "direction", "parameter_set_id", "parameter_snapshot_json", "threshold_mode", "fixed_threshold", "threshold_update_rate", "status", "started_at_epoch", "ended_at_epoch", "error_type", "error_message"],
            "processed_1m_bar": ["run_id", "date", "timestamp", "symbol", "trade_date", "mode", "bar_source", "direction", "parameter_set_id", "trend_window", "slope_std_window", "dev_window", "residual_window", "r2_threshold", "channel_high_percentile", "channel_low_percentile", "continuous_break_count", "active_threshold", "open", "high", "low", "close", "volume", "wap", "bar_count", "bar_size", "what_to_show", "use_rth", "source", "trend_price", "trend_slope", "trend_r2", "trend_slope_rmse", "trend_slope_std", "trend_fit_ok", "trend_raw_trend", "trend_stack_length_after", "channel_pred_high", "channel_pred_low", "channel_effective_trend", "channel_last_trend_slope", "channel_last_trend_intercept", "channel_last_trend_bar_count", "channel_last_high_percentile", "channel_last_low_percentile", "channel_curr_trend_slope", "channel_curr_trend_intercept", "channel_curr_high_percentile", "channel_curr_low_percentile", "channel_stack_length_after", "decision", "decision_recorded_break_count", "decision_triggered"],
            "signal_event": ["run_id", "date", "decision", "price", "break_count"],
            "run_summary": ["run_id", "trade_date", "status", "processed_bar_count", "signal_count", "final_curr_slope", "final_curr_intercept", "final_high_percentile", "final_low_percentile", "final_channel_length", "started_at_epoch", "ended_at_epoch", "error_type", "error_message"],
        }
        actual_tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() if not row[0].startswith("sqlite_")}
        if actual_tables != set(expected):
            return False
        for table, expected_columns in expected.items():
            columns = [row[1] for row in self.connection.execute(f'PRAGMA table_info("{table}")').fetchall()]
            if columns != expected_columns:
                return False
        for table, primary_key in {"single_day_run": ["run_id", "trade_date"], "processed_1m_bar": ["run_id", "date"], "run_summary": ["run_id", "trade_date"], "signal_event": ["run_id", "date"]}.items():
            actual_primary_key = [row[1] for row in self.connection.execute(f'PRAGMA table_info("{table}")').fetchall() if row[5] > 0]
            if actual_primary_key != primary_key:
                return False
        return True

    def _drop_all(self) -> None:
        tables = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for row in tables:
            if not row[0].startswith("sqlite_"):
                self.connection.execute(f'DROP TABLE "{row[0]}"')


def _epoch(value: datetime) -> int:
    return int(value.timestamp())


class SqliteRepositories:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, trade_date: date) -> TradingSession | None:
        row = self.database.connection.execute('SELECT * FROM trade_date WHERE trade_date=?', (trade_date.isoformat(),)).fetchone()
        if row is None:
            return None
        from datetime import timezone
        start = datetime.fromtimestamp(row['session_start_epoch'], timezone.utc) if row['session_start_epoch'] is not None else None
        end = datetime.fromtimestamp(row['session_end_epoch'], timezone.utc) if row['session_end_epoch'] is not None else None
        return TradingSession(trade_date, bool(row['is_trading_day']), start, end)

    def save(self, session: TradingSession) -> None:
        now = int(datetime.now().timestamp())
        with self.database.transaction():
            self.database.connection.execute('''INSERT INTO trade_date VALUES (?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(trade_date) DO UPDATE SET is_trading_day=excluded.is_trading_day,
              session_start_epoch=excluded.session_start_epoch, session_end_epoch=excluded.session_end_epoch,
              source=excluded.source, updated_at_epoch=excluded.updated_at_epoch''',
              (session.trade_date.isoformat(), int(session.is_trading_day), _epoch(session.session_start_et) if session.session_start_et else None,
               _epoch(session.session_end_et) if session.session_end_et else None, 'ibapi_schedule', now, now))

    def load_rth_bars(self, symbol: str, trade_date: date) -> list[RawBar]:
        start = int(datetime.combine(trade_date, datetime.min.time(), session_tz()).timestamp())
        end = start + 2 * 86400
        rows = self.database.connection.execute('SELECT * FROM raw_1m_bar WHERE symbol=? AND date>=? AND date<? ORDER BY date', (symbol, start, end)).fetchall()
        return [RawBar(row['symbol'], row['date'], row['open'], row['high'], row['low'], row['close'], row['volume'], row['wap'], row['bar_count']) for row in rows if datetime.fromtimestamp(row['date'], session_tz()).date() == trade_date]

    def upsert_many(self, bars: Sequence[RawBar], *, bar_size: str, what_to_show: str, use_rth: bool) -> None:
        now = int(datetime.now().timestamp())
        with self.database.transaction():
            self.database.connection.executemany('''INSERT INTO raw_1m_bar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(symbol,date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low,
              close=excluded.close, volume=excluded.volume, wap=excluded.wap, bar_count=excluded.bar_count,
              bar_size=excluded.bar_size, what_to_show=excluded.what_to_show, use_rth=excluded.use_rth,
              source=excluded.source, updated_at_epoch=excluded.updated_at_epoch''',
              [(b.symbol,b.date,b.open,b.high,b.low,b.close,b.volume,b.wap,b.barCount,bar_size,what_to_show,int(use_rth),'ibapi',now,now) for b in bars])

    def create(self, context: RunContext) -> None:
        with self.database.transaction():
            self.database.connection.execute('INSERT INTO single_day_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (context.run_id,context.trade_date.isoformat(),context.symbol,context.mode.value,context.live_phase.value if context.live_phase else None,context.direction.value,context.parameter_set.parameter_set_id,json.dumps(context.parameter_set.__dict__),context.threshold_mode.value,context.fixed_threshold,context.threshold_update_rate,RunStatus.RUNNING.value,_epoch(context.started_at_et),None,None,None))

    def mark_completed(self, run_id: str, trade_date: date, ended_at_et: datetime) -> None: self._mark(run_id, trade_date, RunStatus.COMPLETED, ended_at_et, None)
    def mark_failed(self, run_id: str, trade_date: date, ended_at_et: datetime, error_type: str, error_message: str) -> None: self._mark(run_id, trade_date, RunStatus.FAILED, ended_at_et, (error_type,error_message))
    def mark_skipped(self, context: RunContext, reason: str) -> None:
        self._mark(context.run_id, context.trade_date, RunStatus.SKIPPED, context.started_at_et, ("NonTradingDayError", reason))
    def _mark(self, run_id: str, trade_date: date, status: RunStatus, ended: datetime, error: tuple[str,str] | None) -> None:
        with self.database.transaction(): self.database.connection.execute('UPDATE single_day_run SET status=?, ended_at_epoch=?, error_type=?, error_message=? WHERE run_id=? AND trade_date=?', (status.value,_epoch(ended),error[0] if error else None,error[1] if error else None,run_id,trade_date.isoformat()))

    def insert(self, value: ProcessedBarRecord | SignalEvent) -> None:
        epoch = _epoch(value.timestamp_et)
        if isinstance(value, SignalEvent):
            self.database.connection.execute('INSERT INTO signal_event VALUES (?, ?, ?, ?, ?)', (value.run_id,epoch,value.decision.value,value.price,value.break_count)); return
        parameter = value.parameter_snapshot
        trend = value.trend
        channel = value.channel
        decision = value.decision
        self.database.connection.execute('''
            INSERT INTO processed_1m_bar (
              run_id, date, timestamp, symbol, trade_date, mode, bar_source, direction, parameter_set_id,
              trend_window, slope_std_window, dev_window, residual_window, r2_threshold,
              channel_high_percentile, channel_low_percentile, continuous_break_count,
              active_threshold, open, high, low, close, volume, wap, bar_count,
              bar_size, what_to_show, use_rth, source,
              trend_price, trend_slope, trend_r2, trend_slope_rmse, trend_slope_std,
              trend_fit_ok, trend_raw_trend, trend_stack_length_after,
              channel_pred_high, channel_pred_low, channel_effective_trend,
              channel_last_trend_slope, channel_last_trend_intercept, channel_last_trend_bar_count,
              channel_last_high_percentile, channel_last_low_percentile, channel_curr_trend_slope,
              channel_curr_trend_intercept, channel_curr_high_percentile, channel_curr_low_percentile,
              channel_stack_length_after, decision, decision_recorded_break_count, decision_triggered
            ) VALUES (
              ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?
            )
        ''', (
            value.run_id, epoch, value.timestamp_et.replace(second=0, microsecond=0).isoformat(),
            value.symbol, value.trade_date.isoformat(), value.mode.value,
            value.bar_source.value, value.direction.value, value.parameter_set_id,
            parameter['trend_window'], parameter['slope_std_window'], parameter['dev_window'],
            parameter['residual_window'], parameter['r2_threshold'],
            parameter['channel_high_percentile'], parameter['channel_low_percentile'],
            parameter['continuous_break_count'], value.active_threshold,
            value.open, value.high, value.low, value.close, value.volume, value.wap, value.barCount,
            '1 min', 'TRADES', 1, 'ibapi',
            trend.price, trend.slope, trend.r2, trend.slope_rmse, trend.slope_std,
            None if trend.trend_fit_ok is None else int(trend.trend_fit_ok),
            trend.raw_trend.value if trend.raw_trend is not None else None,
            trend.trend_stack_length_after, channel.pred_high, channel.pred_low,
            channel.effective_trend.value if channel.effective_trend is not None else None,
            channel.last_trend_slope, channel.last_trend_intercept, channel.last_trend_bar_count,
            channel.last_high_percentile, channel.last_low_percentile, channel.curr_trend_slope,
            channel.curr_trend_intercept, channel.curr_high_percentile, channel.curr_low_percentile,
            channel.channel_stack_length_after,
            decision.decision.value if decision.triggered else None,
            decision.recorded_break_count, int(decision.triggered),
        ))

    def save_summary(self, summary: RunSummary) -> None:
        with self.database.transaction(): self.database.connection.execute('INSERT INTO run_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (summary.run_id,summary.trade_date.isoformat(),summary.status.value,summary.processed_bar_count,summary.signal_count,summary.final_curr_slope,summary.final_curr_intercept,summary.final_high_percentile,summary.final_low_percentile,summary.final_channel_length,_epoch(summary.started_at_et),_epoch(summary.ended_at_et),summary.error_type,summary.error_message))

    def complete_with_summary(self, summary: RunSummary) -> None:
        self._save_terminal_summary(summary, RunStatus.COMPLETED)

    def fail_with_summary(self, summary: RunSummary) -> None:
        self._save_terminal_summary(summary, RunStatus.FAILED)

    def _save_terminal_summary(self, summary: RunSummary, status: RunStatus) -> None:
        if summary.status is not status:
            raise PersistenceError(f"Terminal summary status must be {status.value}")
        with self.database.transaction():
            self.database.connection.execute('INSERT INTO run_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (summary.run_id,summary.trade_date.isoformat(),summary.status.value,summary.processed_bar_count,summary.signal_count,summary.final_curr_slope,summary.final_curr_intercept,summary.final_high_percentile,summary.final_low_percentile,summary.final_channel_length,_epoch(summary.started_at_et),_epoch(summary.ended_at_et),summary.error_type,summary.error_message))
            error_type = summary.error_type if status is RunStatus.FAILED else None
            error_message = summary.error_message if status is RunStatus.FAILED else None
            self.database.connection.execute('UPDATE single_day_run SET status=?, ended_at_epoch=?, error_type=?, error_message=? WHERE run_id=? AND trade_date=?', (status.value,_epoch(summary.ended_at_et),error_type,error_message,summary.run_id,summary.trade_date.isoformat()))

    def export_processed_run_csv(self, run_id: str, output_dir: str | Path = Path("data")) -> Path:
        rows = self.database.connection.execute(
            'SELECT * FROM processed_1m_bar WHERE run_id=? ORDER BY date', (run_id,)
        ).fetchall()
        fieldnames = [column[1] for column in self.database.connection.execute('PRAGMA table_info(processed_1m_bar)')]
        destination = Path(output_dir) / f'{run_id}.csv'
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
        return destination


def session_tz() -> ZoneInfo:
    return ZoneInfo('America/New_York')
