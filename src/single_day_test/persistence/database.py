from __future__ import annotations

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

SCHEMA_VERSION = "phase3_ibapi_v1"


class Database:
    """SQLite storage with a deliberately non-compatible Phase 3 schema."""
    def __init__(self, path: str | Path, *, rebuild_legacy: bool = False) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
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
            if version is not None and version[0] == SCHEMA_VERSION:
                return
        has_legacy = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_1m_bar'").fetchone() is not None
        if has_legacy and not self.rebuild_legacy:
            raise PersistenceError("Legacy Phase 2 database detected. Reset it explicitly; automatic migration is unsupported.")
        if has_legacy:
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
          run_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, trade_date TEXT NOT NULL, mode TEXT NOT NULL,
          live_phase TEXT, direction TEXT NOT NULL, parameter_set_id TEXT NOT NULL,
          parameter_snapshot_json TEXT NOT NULL, initial_threshold REAL NOT NULL,
          active_threshold REAL NOT NULL, status TEXT NOT NULL, started_at_epoch INTEGER NOT NULL,
          ended_at_epoch INTEGER, error_type TEXT, error_message TEXT);
        CREATE TABLE processed_1m_bar (
          run_id TEXT NOT NULL, date INTEGER NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
          mode TEXT NOT NULL, bar_source TEXT NOT NULL, direction TEXT NOT NULL, parameter_set_id TEXT NOT NULL,
          parameter_snapshot_json TEXT NOT NULL, initial_threshold REAL NOT NULL, active_threshold REAL NOT NULL,
          open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
          trend_json TEXT NOT NULL, channel_json TEXT NOT NULL, decision_json TEXT NOT NULL,
          PRIMARY KEY(run_id, date));
        CREATE TABLE signal_event (
          run_id TEXT NOT NULL, date INTEGER NOT NULL, decision TEXT NOT NULL, price REAL NOT NULL,
          break_count INTEGER NOT NULL, PRIMARY KEY(run_id, date));
        CREATE TABLE run_summary (
          run_id TEXT PRIMARY KEY, status TEXT NOT NULL, processed_bar_count INTEGER NOT NULL,
          signal_count INTEGER NOT NULL, final_curr_slope REAL, final_curr_intercept REAL,
          final_high_percentile REAL, final_low_percentile REAL, final_channel_length INTEGER NOT NULL,
          started_at_epoch INTEGER NOT NULL, ended_at_epoch INTEGER NOT NULL, error_type TEXT, error_message TEXT);
        ''')
        self.connection.execute("INSERT INTO schema_meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        self.connection.commit()

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
            self.database.connection.execute('INSERT INTO single_day_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (context.run_id,context.symbol,context.trade_date.isoformat(),context.mode.value,context.live_phase.value if context.live_phase else None,context.direction.value,context.parameter_set.parameter_set_id,json.dumps(context.parameter_set.__dict__),context.initial_threshold,context.active_threshold,RunStatus.RUNNING.value,_epoch(context.started_at_et),None,None,None))

    def mark_completed(self, run_id: str, ended_at_et: datetime) -> None: self._mark(run_id, RunStatus.COMPLETED, ended_at_et, None)
    def mark_failed(self, run_id: str, ended_at_et: datetime, error_type: str, error_message: str) -> None: self._mark(run_id, RunStatus.FAILED, ended_at_et, (error_type,error_message))
    def _mark(self, run_id: str, status: RunStatus, ended: datetime, error: tuple[str,str] | None) -> None:
        with self.database.transaction(): self.database.connection.execute('UPDATE single_day_run SET status=?, ended_at_epoch=?, error_type=?, error_message=? WHERE run_id=?', (status.value,_epoch(ended),error[0] if error else None,error[1] if error else None,run_id))

    def insert(self, value: ProcessedBarRecord | SignalEvent) -> None:
        epoch = _epoch(value.timestamp_et)
        if isinstance(value, SignalEvent):
            self.database.connection.execute('INSERT INTO signal_event VALUES (?, ?, ?, ?, ?)', (value.run_id,epoch,value.decision.value,value.price,value.break_count)); return
        self.database.connection.execute('INSERT INTO processed_1m_bar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (value.run_id,epoch,value.symbol,value.trade_date.isoformat(),value.mode.value,value.bar_source.value,value.direction.value,value.parameter_set_id,json.dumps(value.parameter_snapshot),value.initial_threshold,value.active_threshold,value.open,value.high,value.low,value.close,value.volume,json.dumps(value.trend.__dict__,default=str),json.dumps(value.channel.__dict__,default=str),json.dumps(value.decision.__dict__,default=str)))

    def save_summary(self, summary: RunSummary) -> None:
        with self.database.transaction(): self.database.connection.execute('INSERT INTO run_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (summary.run_id,summary.status.value,summary.processed_bar_count,summary.signal_count,summary.final_curr_slope,summary.final_curr_intercept,summary.final_high_percentile,summary.final_low_percentile,summary.final_channel_length,_epoch(summary.started_at_et),_epoch(summary.ended_at_et),summary.error_type,summary.error_message))


def session_tz() -> ZoneInfo:
    return ZoneInfo('America/New_York')
