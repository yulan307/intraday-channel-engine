from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from single_day_test.application.backtest_cli import BacktestScanner, backtest_launch_configuration, resolve_backtest_launch_config
from single_day_test.application.bar_processor import process_bar
from single_day_test.bar_feed.base import FeedEvent
from single_day_test.domain.enums import BarSource, DecisionLabel, Direction, FeedStatus, RunMode, RunStatus, ThresholdMode
from single_day_test.domain.errors import InputValidationError, NonTradingDayError
from single_day_test.domain.models import ChannelBar, ChannelResult, CompletedBar, RawBar, RunContext, TrendBar, TrendResult
from single_day_test.domain.parameters import ParameterSet
from single_day_test.domain.states import ChannelState, RuntimeState, TrendState
from single_day_test.engine.channel_engine import ChannelEngine
from single_day_test.engine.decision_engine import DecisionEngine
from single_day_test.engine.trend_engine import TrendEngine
from single_day_test.persistence.database import Database, SqliteRepositories


ET = ZoneInfo("America/New_York")


def _params() -> ParameterSet:
    return ParameterSet("p1", 3, 3, 0.8, 95.0, 95.0, 1, 1)


def _bar(index: int, price: float = 100.0, opening_price: float | None = None) -> CompletedBar:
    timestamp = datetime(2025, 1, 2, 9, 30, tzinfo=ET) + timedelta(minutes=index)
    return CompletedBar(RawBar("AAPL", int(timestamp.timestamp()), opening_price if opening_price is not None else price, price + 1, price - 1, price, 1, price, 1), BarSource.HIST)


def _context(mode: ThresholdMode) -> RunContext:
    return RunContext("run-1", "AAPL", date(2025, 1, 2), _params(), Direction.BUY, mode, 100.0 if mode is ThresholdMode.FIXED else None, RunMode.BACKTEST, None, datetime(2025, 1, 2, 9, 0, tzinfo=ET))


def test_auto_threshold_uses_first_completed_bar_open() -> None:
    context = _context(ThresholdMode.AUTO)
    state = RuntimeState.empty(context.parameter_set)
    records = []
    for index, price in enumerate((100.0, 101.0, 102.0)):
        transition = process_bar(
            context,
            _bar(index, price, 90.0 if index == 0 else None),
            state,
            TrendEngine(),
            ChannelEngine(),
            DecisionEngine(),
        )
        records.append(transition.record)
        state = transition.next_state_after_persist
    assert [record.active_threshold for record in records] == [90.0, 90.0, 90.0]


def test_auto_with_configured_threshold_uses_that_initial_value() -> None:
    params = _params()
    context = RunContext(
        "run-1", "AAPL", date(2025, 1, 2), params, Direction.BUY,
        ThresholdMode.AUTO, 110.0, RunMode.BACKTEST, None, datetime(2025, 1, 2, 9, 0, tzinfo=ET),
    )
    transition = process_bar(
        context, _bar(0, 100.0, 90.0), RuntimeState.empty(params, context.fixed_threshold),
        TrendEngine(), ChannelEngine(), DecisionEngine(),
    )

    assert transition.record.active_threshold == 110.0


class _SignalTrendEngine:
    def __init__(self, price: float) -> None:
        self.price = price

    def update(self, bar: CompletedBar, state: TrendState, params: ParameterSet) -> tuple[TrendResult, TrendState]:
        next_state = TrendState.empty(params)
        next_state.bars.append(TrendBar(bar.raw.timestamp_et, self.price))
        return TrendResult(self.price, None, None, None, None, None, None, 1), next_state


class _SignalChannelEngine:
    def __init__(self, pred_high: float | None, pred_low: float | None) -> None:
        self.pred_high = pred_high
        self.pred_low = pred_low

    def update(self, bar: CompletedBar, trend: TrendResult, state: ChannelState, params: ParameterSet) -> tuple[ChannelResult, ChannelState]:
        next_state = ChannelState(bars=[ChannelBar(bar.raw.timestamp_et, trend.price, bar.raw.high, bar.raw.low)])
        return ChannelResult(self.pred_high, self.pred_low, None, None, None, None, None, None, None, None, None, None, 1), next_state


@pytest.mark.parametrize(
    ("direction", "previous_threshold", "price", "pred_high", "pred_low", "expected_decision"),
    [
        (Direction.BUY, 110.0, 105.0, 100.0, None, DecisionLabel.BUY),
        (Direction.SELL, 100.0, 105.0, None, 110.0, DecisionLabel.SELL),
    ],
)
def test_auto_signal_updates_threshold_and_resets_trend_and_channel_for_next_bar(
    direction: Direction,
    previous_threshold: float,
    price: float,
    pred_high: float | None,
    pred_low: float | None,
    expected_decision: DecisionLabel,
) -> None:
    params = _params()
    context = RunContext("run-1", "AAPL", date(2025, 1, 2), params, direction, ThresholdMode.AUTO, None, RunMode.BACKTEST, None, datetime(2025, 1, 2, 9, 0, tzinfo=ET))
    state = RuntimeState.empty(params, previous_threshold)
    transition = process_bar(
        context,
        _bar(3, price),
        state,
        _SignalTrendEngine(price),
        _SignalChannelEngine(pred_high, pred_low),
        DecisionEngine(),
    )

    assert transition.record.active_threshold == previous_threshold
    assert transition.record.decision.decision is expected_decision
    assert transition.next_state_after_persist.active_threshold == price
    assert not transition.next_state_after_persist.trend.bars
    assert not transition.next_state_after_persist.trend.valid_slopes
    assert transition.next_state_after_persist.channel == ChannelState.empty()


@pytest.mark.parametrize(
    ("direction", "pred_high", "pred_low", "expected_threshold"),
    [
        (Direction.BUY, 100.0, None, 94.5),
        (Direction.SELL, None, 110.0, 115.5),
    ],
)
def test_auto_signal_applies_directional_threshold_update_rate(
    direction: Direction,
    pred_high: float | None,
    pred_low: float | None,
    expected_threshold: float,
) -> None:
    params = _params()
    context = RunContext(
        "run-1", "AAPL", date(2025, 1, 2), params, direction, ThresholdMode.AUTO,
        None, RunMode.BACKTEST, None, datetime(2025, 1, 2, 9, 0, tzinfo=ET), threshold_update_rate=10.0,
    )
    transition = process_bar(
        context, _bar(3, 105.0), RuntimeState.empty(params, 110.0 if direction is Direction.BUY else 100.0),
        _SignalTrendEngine(105.0), _SignalChannelEngine(pred_high, pred_low), DecisionEngine(),
    )

    assert transition.record.decision.triggered
    assert transition.next_state_after_persist.active_threshold == pytest.approx(expected_threshold)


def test_fixed_signal_preserves_trend_and_channel_state() -> None:
    params = _params()
    context = RunContext("run-1", "AAPL", date(2025, 1, 2), params, Direction.BUY, ThresholdMode.FIXED, 110.0, RunMode.BACKTEST, None, datetime(2025, 1, 2, 9, 0, tzinfo=ET))
    transition = process_bar(
        context,
        _bar(3, 105.0),
        RuntimeState.empty(params, 110.0),
        _SignalTrendEngine(105.0),
        _SignalChannelEngine(100.0, None),
        DecisionEngine(),
    )

    assert transition.record.decision.decision is DecisionLabel.BUY
    assert transition.next_state_after_persist.active_threshold == 110.0
    assert len(transition.next_state_after_persist.trend.bars) == 1
    assert len(transition.next_state_after_persist.channel.bars) == 1


def _backtest_args(config: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "config": config, "symbol": None, "direction": None, "threshold": None,
        "parameter_set_path": None, "parameter_set_id": None, "trade_date_start": None,
        "trade_date_end": None, "ib_environment": None, "database": None, "ib_config": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_yaml_backtest_config_applies_defaults_and_cli_overrides(tmp_path: Path) -> None:
    path = tmp_path / "backtest.yaml"
    path.write_text(
        "symbol: AAPL\ndirection: SELL\nthreshold: 0\nthreshold_update_rate: 12.5\nparameter_set_path: configs/parameter_set.csv\n"
        "parameter_set_id: ''\ntrade_date_start: 2025-01-02\ntrade_date_end: 2025-01-03\n"
            "ib_environment: paper\ndatabase: data/test.sqlite3\nib_config: configs/ib.yaml\nlog_level: INFO\n",
        encoding="utf-8",
    )
    configured = resolve_backtest_launch_config(_backtest_args(path), date(2025, 1, 3))
    assert configured.request.symbol == "AAPL"
    assert configured.request.threshold_mode is ThresholdMode.AUTO
    assert configured.request.fixed_threshold == 0.0
    assert configured.request.threshold_update_rate == 12.5
    assert backtest_launch_configuration(configured)["auto_threshold_enabled"] is True
    assert configured.request.trade_dates == (date(2025, 1, 2), date(2025, 1, 3))
    assert configured.parameter_set_id == ""
    overridden = resolve_backtest_launch_config(
        _backtest_args(
            path, symbol="MSFT", direction="BUY", threshold=150.0,
            parameter_set_path=Path("other.csv"), parameter_set_id="p1",
            trade_date_start=date(2025, 1, 3), trade_date_end=date(2025, 1, 3),
            ib_environment="live", database=Path("other.sqlite3"), ib_config=Path("other-ib.yaml"),
        ),
        date(2025, 1, 3),
    )
    assert overridden.request.symbol == "MSFT"
    assert overridden.request.direction is Direction.BUY
    assert overridden.request.fixed_threshold == 150.0
    assert overridden.request.threshold_mode is ThresholdMode.AUTO
    assert overridden.request.threshold_update_rate == 12.5
    assert overridden.parameter_set_path == Path("other.csv")
    assert overridden.parameter_set_id == "p1"
    assert overridden.request.trade_dates == (date(2025, 1, 3),)
    assert overridden.ib_environment == "live"
    assert overridden.database == Path("other.sqlite3")
    assert overridden.ib_config == Path("other-ib.yaml")

    path.write_text(path.read_text(encoding="utf-8").replace("threshold_update_rate: 12.5", "threshold_update_rate:"), encoding="utf-8")
    without_rate = resolve_backtest_launch_config(_backtest_args(path), date(2025, 1, 3))
    assert without_rate.request.threshold_update_rate == 0.0
    assert without_rate.request.threshold_mode is ThresholdMode.FIXED
    assert backtest_launch_configuration(without_rate)["auto_threshold_enabled"] is False


def test_yaml_backtest_config_rejects_unknown_or_invalid_values(tmp_path: Path) -> None:
    path = tmp_path / "backtest.yaml"
    path.write_text("symbol: AAPL\nunknown: value\n", encoding="utf-8")
    with pytest.raises(InputValidationError, match="unsupported fields"):
        resolve_backtest_launch_config(_backtest_args(path), date(2025, 1, 3))
    path.write_text(
        "symbol: AAPL\ndirection: BUY\nthreshold: ''\nparameter_set_path: params.csv\nparameter_set_id: p1\n"
            "trade_date_start: 2025-01-03\ntrade_date_end: 2025-01-02\nib_environment: paper\ndatabase: test.sqlite3\nib_config: ib.yaml\nlog_level: INFO\n",
        encoding="utf-8",
    )
    with pytest.raises(InputValidationError, match="threshold"):
        resolve_backtest_launch_config(_backtest_args(path), date(2025, 1, 3))
    path.write_text(path.read_text(encoding="utf-8").replace("threshold: ''", "threshold: null"), encoding="utf-8")
    with pytest.raises(InputValidationError, match="must not be later"):
        resolve_backtest_launch_config(_backtest_args(path), date(2025, 1, 3))
    path.write_text(path.read_text(encoding="utf-8").replace("trade_date_end: 2025-01-02", "trade_date_end: 2025-01-04"), encoding="utf-8")
    with pytest.raises(InputValidationError, match="later than the current ET date"):
        resolve_backtest_launch_config(_backtest_args(path), date(2025, 1, 3))
    path.write_text(path.read_text(encoding="utf-8").replace("trade_date_start: 2025-01-03", "trade_date_start: not-a-date").replace("trade_date_end: 2025-01-04", "trade_date_end: null"), encoding="utf-8")
    with pytest.raises(InputValidationError, match="trade_date_start"):
        resolve_backtest_launch_config(_backtest_args(path), date(2025, 1, 3))


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
    aggregate = database.connection.execute("SELECT status, avg_signal_count_per_day, avg_best_reward_per_day, avg_efficiency_per_day FROM run_summary WHERE run_id='run-p1'").fetchone()
    assert tuple(aggregate) == ("COMPLETED", 0.0, None, None)


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
    assert columns[15] == "active_threshold"


def test_run_persists_threshold_update_rate(tmp_path) -> None:
    database = Database(tmp_path / "rate.sqlite3")
    database.initialize()
    repositories = SqliteRepositories(database)
    context = RunContext(
        "run-1", "AAPL", date(2025, 1, 2), _params(), Direction.BUY,
        ThresholdMode.AUTO, None, RunMode.BACKTEST, None, datetime(2025, 1, 2, 9, 0, tzinfo=ET),
        threshold_update_rate=12.5,
    )

    repositories.create(context)

    assert database.connection.execute("SELECT threshold_update_rate FROM single_day_run").fetchone()[0] == 12.5


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
    assert database.connection.execute("SELECT status FROM run_summary WHERE run_id='run-p1'").fetchone()[0] == "FAILED"
    assert sum(1 for _ in (tmp_path / "data" / "run-p1.csv").open(encoding="utf-8")) == 3
