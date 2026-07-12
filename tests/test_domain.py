import pytest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from collections import deque
from dataclasses import FrozenInstanceError

from single_day_test.domain.enums import (
    Direction,
    RunMode,
    LivePhase,
    BarSource,
    FeedStatus,
    TrendLabel,
    DecisionLabel,
    RunStatus,
)
from single_day_test.domain.errors import (
    SingleDayTestError,
    InputValidationError,
    NonTradingDayError,
    InvalidTradeDateError,
    IbApiError,
    HistoricalDataError,
    BarValidationError,
    BarOrderingError,
    AlgorithmError,
    PersistenceError,
)
from single_day_test.domain.parameters import ParameterSet, validate_parameter_set
from single_day_test.domain.models import (
    RunRequest,
    RunContext,
    RawBar,
    CompletedBar,
    TrendBar,
    ChannelBar,
    TradingSession,
    TrendResult,
    ChannelResult,
    DecisionResult,
    ProcessedBarRecord,
    SignalEvent,
    RunSummary,
)
from single_day_test.domain.states import TrendState, ChannelState, DecisionState, RuntimeState
from single_day_test.bar_feed.base import FeedEvent


ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=ET)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_params(**overrides):
    base = dict(
        parameter_set_id="p1",
        trend_window=30,
        slope_std_window=10,
        dev_window=5,
        residual_window=5,
        r2_threshold=0.5,
        channel_high_percentile=95.0,
        channel_low_percentile=95.0,
        continuous_break_count=3,
    )
    base.update(overrides)
    return ParameterSet(**base)


def _make_rawbar(ts=None, **kw):
    if ts is None:
        ts = datetime(2025, 1, 15, 10, 0, tzinfo=ET)
    base = dict(symbol="AAPL", date=int(ts.timestamp()), open=100.0, high=101.0, low=99.0,
                close=100.5, volume=1000, wap=100.25, barCount=10)
    base.update(kw)
    return RawBar(**base)


def _params():
    return _make_params()


PRICE = 100.5
HIGH_VAL = 105.0
LOW_VAL = 95.0


# ---------------------------------------------------------------------------
# 2. Enum values + error hierarchy
# ---------------------------------------------------------------------------
def test_enums_values():
    assert Direction.BUY.value == "BUY"
    assert Direction.SELL.value == "SELL"
    assert RunMode.BACKTEST.value == "BACKTEST"
    assert RunMode.LIVE_PAPER.value == "LIVE_PAPER"
    assert LivePhase.PRE_MARKET_WAIT.value == "PRE_MARKET_WAIT"
    assert LivePhase.IN_SESSION.value == "IN_SESSION"
    assert BarSource.HIST.value == "HIST"
    assert BarSource.LIVE.value == "LIVE"
    assert FeedStatus.BAR_AVAILABLE.value == "bar_available"
    assert FeedStatus.BAR_WAITING.value == "bar_waiting"
    assert FeedStatus.BAR_END.value == "bar_end"
    assert TrendLabel.UP.value == "UP"
    assert TrendLabel.DOWN.value == "DOWN"
    assert TrendLabel.SIDEWAY.value == "SIDEWAY"
    assert DecisionLabel.BUY.value == "BUY"
    assert DecisionLabel.NO_BUY.value == "NO_BUY"
    assert DecisionLabel.SELL.value == "SELL"
    assert DecisionLabel.NO_SELL.value == "NO_SELL"
    assert RunStatus.RUNNING.value == "RUNNING"
    assert RunStatus.COMPLETED.value == "COMPLETED"
    assert RunStatus.FAILED.value == "FAILED"


def test_error_hierarchy():
    assert issubclass(InputValidationError, SingleDayTestError)
    assert issubclass(NonTradingDayError, SingleDayTestError)
    assert issubclass(InvalidTradeDateError, SingleDayTestError)
    assert issubclass(IbApiError, SingleDayTestError)
    assert issubclass(HistoricalDataError, SingleDayTestError)
    assert issubclass(BarValidationError, SingleDayTestError)
    assert issubclass(BarOrderingError, SingleDayTestError)
    assert issubclass(AlgorithmError, SingleDayTestError)
    assert issubclass(PersistenceError, SingleDayTestError)


# ---------------------------------------------------------------------------
# 3. ParameterSet validation
# ---------------------------------------------------------------------------
def test_parameter_set_valid_boundaries():
    validate_parameter_set(_make_params(trend_window=3))
    validate_parameter_set(_make_params(slope_std_window=2))
    validate_parameter_set(_make_params(r2_threshold=0.0))
    validate_parameter_set(_make_params(r2_threshold=1.0))
    validate_parameter_set(_make_params(channel_high_percentile=0.0))
    validate_parameter_set(_make_params(channel_high_percentile=100.0))
    validate_parameter_set(_make_params(channel_low_percentile=0.0))
    validate_parameter_set(_make_params(channel_low_percentile=100.0))
    validate_parameter_set(_make_params(continuous_break_count=1))


def test_parameter_set_invalid_trend_window():
    with pytest.raises(InputValidationError, match="trend_window"):
        validate_parameter_set(_make_params(trend_window=2))


def test_parameter_set_invalid_slope_std_window():
    with pytest.raises(InputValidationError, match="slope_std_window"):
        validate_parameter_set(_make_params(slope_std_window=1))


def test_parameter_set_invalid_r2_below():
    with pytest.raises(InputValidationError, match="r2_threshold"):
        validate_parameter_set(_make_params(r2_threshold=-0.1))


def test_parameter_set_invalid_r2_above():
    with pytest.raises(InputValidationError, match="r2_threshold"):
        validate_parameter_set(_make_params(r2_threshold=1.1))


def test_parameter_set_invalid_high_percentile_below():
    with pytest.raises(InputValidationError, match="channel_high_percentile"):
        validate_parameter_set(_make_params(channel_high_percentile=-0.1))


def test_parameter_set_invalid_high_percentile_above():
    with pytest.raises(InputValidationError, match="channel_high_percentile"):
        validate_parameter_set(_make_params(channel_high_percentile=100.1))


def test_parameter_set_invalid_low_percentile_below():
    with pytest.raises(InputValidationError, match="channel_low_percentile"):
        validate_parameter_set(_make_params(channel_low_percentile=-0.1))


def test_parameter_set_invalid_low_percentile_above():
    with pytest.raises(InputValidationError, match="channel_low_percentile"):
        validate_parameter_set(_make_params(channel_low_percentile=100.1))


def test_parameter_set_invalid_continuous_break_count():
    with pytest.raises(InputValidationError, match="continuous_break_count"):
        validate_parameter_set(_make_params(continuous_break_count=0))


# ---------------------------------------------------------------------------
# 1. Instantiate every Phase‑0 dataclass
# ---------------------------------------------------------------------------
def test_instantiate_all_dataclasses():
    params = _params()
    # RunRequest
    RunRequest(
        symbol="AAPL",
        trade_date=date(2025, 1, 15),
        parameter_set=params,
        direction=Direction.BUY,
        initial_threshold=0.0,
    )
    # RunContext
    RunContext(
        run_id="r1",
        symbol="AAPL",
        trade_date=date(2025, 1, 15),
        parameter_set=params,
        direction=Direction.BUY,
        initial_threshold=0.0,
        active_threshold=0.0,
        mode=RunMode.BACKTEST,
        live_phase=None,
        started_at_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
    )
    # RawBar
    raw = _make_rawbar()
    # CompletedBar
    CompletedBar(raw=raw, source=BarSource.HIST)
    # TrendBar
    TrendBar(
        timestamp_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        price=PRICE,
    )
    # ChannelBar
    ChannelBar(
        timestamp_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        price=PRICE,
        high=HIGH_VAL,
        low=LOW_VAL,
    )
    # TradingSession
    TradingSession(
        trade_date=date(2025, 1, 15),
        is_trading_day=True,
        session_start_et=datetime(2025, 1, 15, 9, 30, tzinfo=ET),
        session_end_et=datetime(2025, 1, 15, 16, 0, tzinfo=ET),
    )
    # TrendResult
    TrendResult(
        price=PRICE,
        slope=0.5,
        r2=0.8,
        slope_rmse=0.1,
        slope_std=0.2,
        trend_fit_ok=True,
        raw_trend=TrendLabel.UP,
        trend_stack_length_after=30,
    )
    # ChannelResult
    ChannelResult(
        pred_high=HIGH_VAL,
        pred_low=LOW_VAL,
        effective_trend=TrendLabel.UP,
        last_trend_slope=0.5,
        last_trend_intercept=100.0,
        last_trend_bar_count=30,
        last_high_percentile=95.0,
        last_low_percentile=5.0,
        curr_trend_slope=0.4,
        curr_trend_intercept=100.5,
        curr_high_percentile=94.0,
        curr_low_percentile=6.0,
        channel_stack_length_after=30,
    )
    # DecisionResult
    DecisionResult(
        decision=DecisionLabel.BUY,
        recorded_break_count=3,
        triggered=True,
    )
    # ProcessedBarRecord
    trend = TrendResult(
        price=PRICE,
        slope=0.5,
        r2=0.8,
        slope_rmse=0.1,
        slope_std=0.2,
        trend_fit_ok=True,
        raw_trend=TrendLabel.UP,
        trend_stack_length_after=30,
    )
    channel = ChannelResult(
        pred_high=HIGH_VAL,
        pred_low=LOW_VAL,
        effective_trend=TrendLabel.UP,
        last_trend_slope=0.5,
        last_trend_intercept=100.0,
        last_trend_bar_count=30,
        last_high_percentile=95.0,
        last_low_percentile=5.0,
        curr_trend_slope=0.4,
        curr_trend_intercept=100.5,
        curr_high_percentile=94.0,
        curr_low_percentile=6.0,
        channel_stack_length_after=30,
    )
    decision = DecisionResult(
        decision=DecisionLabel.BUY,
        recorded_break_count=3,
        triggered=True,
    )
    ProcessedBarRecord(
        run_id="r1",
        symbol="AAPL",
        trade_date=date(2025, 1, 15),
        timestamp_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        mode=RunMode.BACKTEST,
        bar_source=BarSource.HIST,
        direction=Direction.BUY,
        parameter_set_id="p1",
        parameter_snapshot={},
        initial_threshold=0.0,
        active_threshold=0.0,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000,
        trend=trend,
        channel=channel,
        decision=decision,
    )
    # SignalEvent
    SignalEvent(
        run_id="r1",
        timestamp_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        decision=DecisionLabel.BUY,
        price=PRICE,
        break_count=3,
    )
    # RunSummary
    RunSummary(
        run_id="r1",
        symbol="AAPL",
        trade_date=date(2025, 1, 15),
        mode=RunMode.BACKTEST,
        direction=Direction.BUY,
        parameter_set_id="p1",
        parameter_snapshot={},
        initial_threshold=0.0,
        processed_bar_count=390,
        signal_count=1,
        final_curr_slope=0.5,
        final_curr_intercept=100.0,
        final_high_percentile=95.0,
        final_low_percentile=5.0,
        final_channel_length=30,
        status=RunStatus.COMPLETED,
        started_at_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        ended_at_et=datetime(2025, 1, 15, 16, 0, tzinfo=ET),
        error_type=None,
        error_message=None,
    )
    # TrendState / ChannelState / DecisionState / RuntimeState
    TrendState.empty(params)
    ChannelState.empty()
    DecisionState()
    RuntimeState.empty(params)
    # FeedEvent
    cb = CompletedBar(raw=raw, source=BarSource.HIST)
    FeedEvent(status=FeedStatus.BAR_AVAILABLE, bar=cb)


# ---------------------------------------------------------------------------
# 4. Datetime behaviour
# ---------------------------------------------------------------------------
def _runctx_factory(dt):
    return RunContext(
        run_id="r1", symbol="AAPL", trade_date=date(2025, 1, 15),
        parameter_set=_params(), direction=Direction.BUY,
        initial_threshold=0.0, active_threshold=0.0,
        mode=RunMode.BACKTEST, live_phase=None, started_at_et=dt,
    )


def _rawbar_factory(dt):
    return RawBar(symbol="AAPL", date=int(dt.timestamp()), open=100.0, high=101.0,
                  low=99.0, close=100.5, volume=1000, wap=100.25, barCount=10)


def _trendbar_factory(dt):
    return TrendBar(timestamp_et=dt, price=PRICE)


def _channelbar_factory(dt):
    return ChannelBar(timestamp_et=dt, price=PRICE, high=HIGH_VAL, low=LOW_VAL)


def _tssession_start_factory(dt):
    return TradingSession(
        trade_date=date(2025, 1, 15), is_trading_day=True,
        session_start_et=dt,
        session_end_et=datetime(2025, 1, 15, 16, 0, tzinfo=ET),
    )


def _tssession_end_factory(dt):
    return TradingSession(
        trade_date=date(2025, 1, 15), is_trading_day=True,
        session_start_et=datetime(2025, 1, 15, 9, 30, tzinfo=ET),
        session_end_et=dt,
    )


def _pbrecord_factory(dt):
    trend = TrendResult(price=PRICE, slope=0.5, r2=0.8, slope_rmse=0.1,
                        slope_std=0.2, trend_fit_ok=True,
                        raw_trend=TrendLabel.UP, trend_stack_length_after=30)
    channel = ChannelResult(pred_high=HIGH_VAL, pred_low=LOW_VAL,
                            effective_trend=TrendLabel.UP,
                            last_trend_slope=0.5, last_trend_intercept=100.0,
                            last_trend_bar_count=30, last_high_percentile=95.0,
                            last_low_percentile=5.0, curr_trend_slope=0.4,
                            curr_trend_intercept=100.5,
                            curr_high_percentile=94.0, curr_low_percentile=6.0,
                            channel_stack_length_after=30)
    decision = DecisionResult(decision=DecisionLabel.BUY,
                              recorded_break_count=3, triggered=True)
    return ProcessedBarRecord(
        run_id="r1", symbol="AAPL", trade_date=date(2025, 1, 15),
        timestamp_et=dt, mode=RunMode.BACKTEST, bar_source=BarSource.HIST,
        direction=Direction.BUY, parameter_set_id="p1",
        parameter_snapshot={}, initial_threshold=0.0, active_threshold=0.0,
        open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
        trend=trend, channel=channel, decision=decision,
    )


def _signalfactory(dt):
    return SignalEvent(run_id="r1", timestamp_et=dt,
                       decision=DecisionLabel.BUY, price=PRICE, break_count=3)


def _runsumm_start_factory(dt):
    return RunSummary(
        run_id="r1", symbol="AAPL", trade_date=date(2025, 1, 15),
        mode=RunMode.BACKTEST, direction=Direction.BUY,
        parameter_set_id="p1", parameter_snapshot={},
        initial_threshold=0.0, processed_bar_count=390, signal_count=1,
        final_curr_slope=0.5, final_curr_intercept=100.0,
        final_high_percentile=95.0, final_low_percentile=5.0,
        final_channel_length=30, status=RunStatus.COMPLETED,
        started_at_et=dt,
        ended_at_et=datetime(2025, 1, 15, 16, 0, tzinfo=ET),
        error_type=None, error_message=None,
    )


def _runsumm_end_factory(dt):
    return RunSummary(
        run_id="r1", symbol="AAPL", trade_date=date(2025, 1, 15),
        mode=RunMode.BACKTEST, direction=Direction.BUY,
        parameter_set_id="p1", parameter_snapshot={},
        initial_threshold=0.0, processed_bar_count=390, signal_count=1,
        final_curr_slope=0.5, final_curr_intercept=100.0,
        final_high_percentile=95.0, final_low_percentile=5.0,
        final_channel_length=30, status=RunStatus.COMPLETED,
        started_at_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        ended_at_et=dt,
        error_type=None, error_message=None,
    )


DATETIME_CASES = [
    (_runctx_factory, "started_at_et"),
    (_trendbar_factory, "timestamp_et"),
    (_channelbar_factory, "timestamp_et"),
    (_tssession_start_factory, "session_start_et"),
    (_tssession_end_factory, "session_end_et"),
    (_pbrecord_factory, "timestamp_et"),
    (_signalfactory, "timestamp_et"),
    (_runsumm_start_factory, "started_at_et"),
    (_runsumm_end_factory, "ended_at_et"),
]


@pytest.mark.parametrize("factory,field", DATETIME_CASES)
def test_datetime_aware_et(factory, field):
    dt_et = datetime(2025, 1, 15, 10, 0, tzinfo=ET)
    obj = factory(dt_et)
    assert getattr(obj, field) == dt_et


@pytest.mark.parametrize("factory,field", DATETIME_CASES)
def test_datetime_utc_converts_to_et(factory, field):
    dt_utc = datetime(2025, 1, 15, 15, 0, tzinfo=UTC)
    obj = factory(dt_utc)
    expected = dt_utc.astimezone(ET)
    assert getattr(obj, field) == expected


@pytest.mark.parametrize("factory", [f[0] for f in DATETIME_CASES],
                         ids=lambda f: f.__name__ if hasattr(f, "__name__") else str(f))
def test_datetime_naive_raises(factory):
    dt_naive = datetime(2025, 1, 15, 10, 0)
    with pytest.raises(InputValidationError):
        factory(dt_naive)


def test_trading_session_optional_none():
    ts = TradingSession(
        trade_date=date(2025, 1, 15),
        is_trading_day=True,
        session_start_et=None,
        session_end_et=None,
    )
    assert ts.session_start_et is None
    assert ts.session_end_et is None


# ---------------------------------------------------------------------------
# 5. Immutability / mutability
# ---------------------------------------------------------------------------
def test_immutable_dataclasses_reject_assignment():
    params = _params()
    raw = _make_rawbar()
    # RunRequest
    req = RunRequest(
        symbol="AAPL",
        trade_date=date(2025, 1, 15),
        parameter_set=params,
        direction=Direction.BUY,
        initial_threshold=0.0,
    )
    with pytest.raises(FrozenInstanceError):
        req.symbol = "MSFT"
    # RunContext
    ctx = RunContext(
        run_id="r1",
        symbol="AAPL",
        trade_date=date(2025, 1, 15),
        parameter_set=params,
        direction=Direction.BUY,
        initial_threshold=0.0,
        active_threshold=0.0,
        mode=RunMode.BACKTEST,
        live_phase=None,
        started_at_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
    )
    with pytest.raises(FrozenInstanceError):
        ctx.run_id = "r2"
    # RawBar
    with pytest.raises(FrozenInstanceError):
        raw.symbol = "MSFT"
    # CompletedBar
    cb = CompletedBar(raw=raw, source=BarSource.HIST)
    with pytest.raises(FrozenInstanceError):
        cb.source = BarSource.LIVE
    # TrendBar
    tb = TrendBar(
        timestamp_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        price=PRICE,
    )
    with pytest.raises(FrozenInstanceError):
        tb.price = 101.0
    # ChannelBar
    chb = ChannelBar(
        timestamp_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        price=PRICE,
        high=HIGH_VAL,
        low=LOW_VAL,
    )
    with pytest.raises(FrozenInstanceError):
        chb.price = 102.0
    # TradingSession
    ts = TradingSession(
        trade_date=date(2025, 1, 15),
        is_trading_day=True,
        session_start_et=datetime(2025, 1, 15, 9, 30, tzinfo=ET),
        session_end_et=datetime(2025, 1, 15, 16, 0, tzinfo=ET),
    )
    with pytest.raises(FrozenInstanceError):
        ts.trade_date = date(2025, 1, 16)
    # TrendResult
    tr = TrendResult(
        price=PRICE, slope=0.5, r2=0.8, slope_rmse=0.1, slope_std=0.2,
        trend_fit_ok=True, raw_trend=TrendLabel.UP, trend_stack_length_after=30,
    )
    with pytest.raises(FrozenInstanceError):
        tr.slope = 0.6
    # ChannelResult
    cr = ChannelResult(
        pred_high=HIGH_VAL, pred_low=LOW_VAL, effective_trend=TrendLabel.UP,
        last_trend_slope=0.5, last_trend_intercept=100.0,
        last_trend_bar_count=30, last_high_percentile=95.0,
        last_low_percentile=5.0, curr_trend_slope=0.4,
        curr_trend_intercept=100.5, curr_high_percentile=94.0,
        curr_low_percentile=6.0, channel_stack_length_after=30,
    )
    with pytest.raises(FrozenInstanceError):
        cr.pred_high = 106.0
    # DecisionResult
    dr = DecisionResult(decision=DecisionLabel.BUY, recorded_break_count=3, triggered=True)
    with pytest.raises(FrozenInstanceError):
        dr.recorded_break_count = 4
    # ProcessedBarRecord
    trend = TrendResult(price=PRICE, slope=0.5, r2=0.8, slope_rmse=0.1,
                        slope_std=0.2, trend_fit_ok=True,
                        raw_trend=TrendLabel.UP, trend_stack_length_after=30)
    channel = ChannelResult(pred_high=HIGH_VAL, pred_low=LOW_VAL,
                            effective_trend=TrendLabel.UP,
                            last_trend_slope=0.5, last_trend_intercept=100.0,
                            last_trend_bar_count=30, last_high_percentile=95.0,
                            last_low_percentile=5.0, curr_trend_slope=0.4,
                            curr_trend_intercept=100.5,
                            curr_high_percentile=94.0, curr_low_percentile=6.0,
                            channel_stack_length_after=30)
    decision = DecisionResult(decision=DecisionLabel.BUY,
                              recorded_break_count=3, triggered=True)
    pbr = ProcessedBarRecord(
        run_id="r1", symbol="AAPL", trade_date=date(2025, 1, 15),
        timestamp_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        mode=RunMode.BACKTEST, bar_source=BarSource.HIST,
        direction=Direction.BUY, parameter_set_id="p1",
        parameter_snapshot={}, initial_threshold=0.0, active_threshold=0.0,
        open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
        trend=trend, channel=channel, decision=decision,
    )
    with pytest.raises(FrozenInstanceError):
        pbr.run_id = "r2"
    # SignalEvent
    se = SignalEvent(
        run_id="r1",
        timestamp_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        decision=DecisionLabel.BUY,
        price=PRICE,
        break_count=3,
    )
    with pytest.raises(FrozenInstanceError):
        se.run_id = "r2"
    # RunSummary
    rs = RunSummary(
        run_id="r1", symbol="AAPL", trade_date=date(2025, 1, 15),
        mode=RunMode.BACKTEST, direction=Direction.BUY,
        parameter_set_id="p1", parameter_snapshot={},
        initial_threshold=0.0, processed_bar_count=390, signal_count=1,
        final_curr_slope=0.5, final_curr_intercept=100.0,
        final_high_percentile=95.0, final_low_percentile=5.0,
        final_channel_length=30, status=RunStatus.COMPLETED,
        started_at_et=datetime(2025, 1, 15, 10, 0, tzinfo=ET),
        ended_at_et=datetime(2025, 1, 15, 16, 0, tzinfo=ET),
        error_type=None, error_message=None,
    )
    with pytest.raises(FrozenInstanceError):
        rs.run_id = "r2"
    # FeedEvent
    fe = FeedEvent(status=FeedStatus.BAR_AVAILABLE, bar=cb)
    with pytest.raises(FrozenInstanceError):
        fe.status = FeedStatus.BAR_WAITING


def test_mutable_states():
    params = _params()
    ts = TrendState.empty(params)
    ts.bars.append(None)
    cs = ChannelState.empty()
    cs.bars.append(None)
    ds = DecisionState()
    ds.break_count = 1
    rs = RuntimeState.empty(params)
    rs.processed_bar_count = 1


def test_runtime_state_independence():
    params = _params()
    rs1 = RuntimeState.empty(params)
    rs2 = RuntimeState.empty(params)
    rs1.signal_events.append("a")
    rs2.channel.bars.append("b")
    assert rs2.signal_events == []
    assert rs1.channel.bars == []


# ---------------------------------------------------------------------------
# 6. State factory defaults / maxlen
# ---------------------------------------------------------------------------
def test_trend_state_empty():
    params = _params()
    ts = TrendState.empty(params)
    assert ts.bars.maxlen == 30
    assert ts.valid_slopes.maxlen == 10
    assert len(ts.bars) == 0
    assert len(ts.valid_slopes) == 0


def test_channel_state_empty():
    cs = ChannelState.empty()
    assert cs.bars == []
    assert cs.effective_trend is None
    assert cs.last_trend_slope is None
    assert cs.last_trend_intercept is None
    assert cs.last_trend_bar_count is None
    assert cs.last_high_percentile is None
    assert cs.last_low_percentile is None
    assert cs.curr_trend_slope is None
    assert cs.curr_trend_intercept is None
    assert cs.curr_high_percentile is None
    assert cs.curr_low_percentile is None


def test_runtime_state_empty():
    params = _params()
    rs = RuntimeState.empty(params)
    assert rs.trend.bars.maxlen == 30
    assert rs.trend.valid_slopes.maxlen == 10
    assert rs.channel.bars == []
    assert rs.decision.break_count == 0
    assert rs.decision_complete is True
    assert rs.processed_bar_count == 0
    assert rs.signal_events == []


# ---------------------------------------------------------------------------
# 7. FeedEvent validation
# ---------------------------------------------------------------------------
def test_feed_event_validation():
    raw = _make_rawbar()
    cb = CompletedBar(raw=raw, source=BarSource.HIST)
    # valid
    ev = FeedEvent(status=FeedStatus.BAR_AVAILABLE, bar=cb)
    assert ev.status is FeedStatus.BAR_AVAILABLE
    assert ev.bar is cb

    ev2 = FeedEvent(status=FeedStatus.BAR_WAITING, bar=None)
    assert ev2.status is FeedStatus.BAR_WAITING

    ev3 = FeedEvent(status=FeedStatus.BAR_END, bar=None)
    assert ev3.status is FeedStatus.BAR_END

    # invalid
    with pytest.raises(InputValidationError):
        FeedEvent(status=FeedStatus.BAR_AVAILABLE, bar=None)
    with pytest.raises(InputValidationError):
        FeedEvent(status=FeedStatus.BAR_WAITING, bar=cb)
    with pytest.raises(InputValidationError):
        FeedEvent(status=FeedStatus.BAR_END, bar=cb)


# ---------------------------------------------------------------------------
# 8. Protocol smoke
# ---------------------------------------------------------------------------
def test_protocol_imports():
    from single_day_test.support.clock import Clock
    from single_day_test.support.ids import IdGenerator
    from single_day_test.support.logging import StructuredLogger
    from single_day_test.bar_feed.base import BarFeed
    from single_day_test.ib.gateway import IbGateway, SubscriptionHandle
    from single_day_test.persistence.trade_date_repository import TradeDateRepository
    from single_day_test.persistence.raw_bar_repository import RawBarRepository
    from single_day_test.persistence.run_repository import RunRepository
    from single_day_test.persistence.processed_bar_repository import ProcessedBarRepository
    from single_day_test.persistence.signal_repository import SignalRepository
    from single_day_test.persistence.summary_repository import SummaryRepository

    assert hasattr(Clock, "now_et")
    assert hasattr(IdGenerator, "new_run_id")
    assert hasattr(StructuredLogger, "info")
    assert hasattr(StructuredLogger, "error")
    assert hasattr(BarFeed, "start")
    assert hasattr(BarFeed, "next_event")
    assert hasattr(BarFeed, "close")
    assert hasattr(IbGateway, "query_trading_session")
    assert hasattr(IbGateway, "request_historical_1m_bars")
    assert hasattr(IbGateway, "subscribe_completed_1m_bars")
    assert hasattr(SubscriptionHandle, "close")
    assert hasattr(TradeDateRepository, "get")
    assert hasattr(TradeDateRepository, "save")
    assert hasattr(RawBarRepository, "load_rth_bars")
    assert hasattr(RawBarRepository, "upsert_many")
    assert hasattr(RunRepository, "create")
    assert hasattr(RunRepository, "mark_completed")
    assert hasattr(RunRepository, "mark_failed")
    assert hasattr(ProcessedBarRepository, "insert")
    assert hasattr(SignalRepository, "insert")
    assert hasattr(SummaryRepository, "save")
