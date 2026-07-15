from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from single_day_test.domain.enums import BarSource, DecisionLabel, Direction, TrendLabel
from single_day_test.domain.errors import InputValidationError
from single_day_test.domain.models import ChannelBar, CompletedBar, RawBar, TrendResult
from single_day_test.domain.parameters import ParameterSet
from single_day_test.domain.states import ChannelState, DecisionState, TrendState
from single_day_test.engine.channel_engine import ChannelEngine, calculate_current_model
from single_day_test.engine.decision_engine import DecisionEngine
from single_day_test.engine.regression import linear_regression
from single_day_test.engine.trend_engine import TrendEngine, classify_raw_trend


ET = ZoneInfo("America/New_York")


def params(**overrides: object) -> ParameterSet:
    values: dict[str, object] = {
        "parameter_set_id": "phase-1",
        "trend_window": 3,
        "channel_window": 3,
        "r2_threshold": 0.8,
        "channel_high_percentile": 50.0,
        "channel_low_percentile": 50.0,
        "continuous_break_count": 2,
    }
    values.update(overrides)
    return ParameterSet(**values)


def completed_bar(index: int, price: float) -> CompletedBar:
    timestamp = datetime(2025, 1, 15, 9, 30, tzinfo=ET) + timedelta(minutes=index)
    return CompletedBar(
        raw=RawBar(
            symbol="AAPL",
            date=int(timestamp.timestamp()),
            open=price,
            high=price + 1.0,
            low=price - 1.0,
            close=price,
            volume=100.0,
            wap=price,
            barCount=1,
        ),
        source=BarSource.HIST,
    )


def trend(price: float, raw_trend: TrendLabel | None) -> TrendResult:
    return TrendResult(
        price=price,
        slope=None,
        r2=None,
        slope_rmse=None,
        slope_std=None,
        trend_fit_ok=None,
        raw_trend=raw_trend,
        trend_stack_length_after=0,
    )


def test_linear_regression_returns_expected_metrics() -> None:
    result = linear_regression(
        np.array([0.0, 1.0, 2.0]), np.array([1.0, 3.0, 5.0])
    )

    assert result.slope == pytest.approx(2.0)
    assert result.intercept == pytest.approx(1.0)
    assert result.r2 == pytest.approx(1.0)
    assert result.rmse == pytest.approx(0.0)
    assert result.predicted == pytest.approx(np.array([1.0, 3.0, 5.0]))


@pytest.mark.parametrize(
    ("x", "y"),
    [
        (np.array([0.0]), np.array([1.0])),
        (np.array([0.0, 1.0]), np.array([1.0])),
        (np.array([0.0, np.nan]), np.array([1.0, 2.0])),
        (np.array([1.0, 1.0]), np.array([1.0, 2.0])),
    ],
)
def test_linear_regression_rejects_invalid_input(
    x: np.ndarray, y: np.ndarray
) -> None:
    with pytest.raises(InputValidationError):
        linear_regression(x, y)


def test_trend_engine_warmup_then_classifies_and_preserves_input_state() -> None:
    engine = TrendEngine()
    state = TrendState.empty(params())

    first, state_after_first = engine.update(completed_bar(0, 1.0), state, params())
    second, state_after_second = engine.update(
        completed_bar(1, 2.0), state_after_first, params()
    )
    third, state_after_third = engine.update(
        completed_bar(2, 3.0), state_after_second, params()
    )
    fourth, state_after_fourth = engine.update(
        completed_bar(3, 4.0), state_after_third, params()
    )

    assert first.slope is None
    assert second.slope is None
    assert third.slope == pytest.approx(1.0)
    assert third.slope_std is None
    assert third.raw_trend is None
    assert fourth.slope_std == pytest.approx(0.0)
    assert fourth.raw_trend is TrendLabel.UP
    assert [item.price for item in state.bars] == []
    assert [item.price for item in state_after_fourth.bars] == [2.0, 3.0, 4.0]
    fifth, state_after_fifth = engine.update(
        completed_bar(4, 5.0), state_after_fourth, params()
    )
    _, state_after_sixth = engine.update(
        completed_bar(5, 6.0), state_after_fifth, params()
    )
    assert fifth.slope_std == pytest.approx(0.0)
    assert state_after_sixth.valid_slopes.maxlen == 3
    assert len(state_after_sixth.valid_slopes) == 3


def test_trend_classification_covers_all_labels() -> None:
    assert classify_raw_trend(1.1, 1.0, 0.9, 0.8) is TrendLabel.UP
    assert classify_raw_trend(-1.1, 1.0, 0.9, 0.8) is TrendLabel.DOWN
    assert classify_raw_trend(0.5, 1.0, 0.9, 0.8) is TrendLabel.SIDEWAY
    assert classify_raw_trend(2.0, 1.0, 0.7, 0.8) is None


def test_channel_current_model_uses_latest_x_origin_and_absolute_deviation() -> None:
    bars = [
        ChannelBar(datetime(2025, 1, 15, 9, 30, tzinfo=ET), 1.0, 2.0, 0.0),
        ChannelBar(datetime(2025, 1, 15, 9, 31, tzinfo=ET), 2.0, 3.0, 1.0),
        ChannelBar(datetime(2025, 1, 15, 9, 32, tzinfo=ET), 3.0, 4.0, 2.0),
    ]

    model = calculate_current_model(bars, params())

    assert model is not None
    assert model.slope == pytest.approx(1.0)
    assert model.intercept == pytest.approx(3.0)
    assert model.high_percentile == pytest.approx(1.0)
    assert model.low_percentile == pytest.approx(1.0)


def test_channel_switch_keeps_current_prediction_and_promotes_old_model() -> None:
    old_bars = [
        ChannelBar(datetime(2025, 1, 15, 9, 30, tzinfo=ET), 1.0, 2.0, 0.0),
        ChannelBar(datetime(2025, 1, 15, 9, 31, tzinfo=ET), 2.0, 3.0, 1.0),
        ChannelBar(datetime(2025, 1, 15, 9, 32, tzinfo=ET), 3.0, 4.0, 2.0),
    ]
    state = ChannelState(
        bars=old_bars,
        effective_trend=TrendLabel.UP,
        last_trend_slope=10.0,
        last_trend_intercept=100.0,
        last_trend_bar_count=2,
        last_high_percentile=5.0,
        last_low_percentile=4.0,
        curr_trend_slope=1.0,
        curr_trend_intercept=3.0,
        curr_high_percentile=1.0,
        curr_low_percentile=1.0,
    )

    result, next_state = ChannelEngine().update(
        completed_bar(3, 4.0), trend(4.0, TrendLabel.DOWN), state, params()
    )

    assert result.pred_high == pytest.approx(135.0)
    assert result.pred_low == pytest.approx(126.0)
    assert result.effective_trend is TrendLabel.DOWN
    assert result.last_trend_slope == pytest.approx(1.0)
    assert result.last_trend_intercept == pytest.approx(3.0)
    assert result.last_trend_bar_count == 1
    assert result.channel_stack_length_after == 1
    assert [item.price for item in state.bars] == [1.0, 2.0, 3.0]
    assert [item.price for item in next_state.bars] == [4.0]


def test_channel_null_raw_trend_continues_existing_segment() -> None:
    state = ChannelState(
        bars=[ChannelBar(datetime(2025, 1, 15, 9, 30, tzinfo=ET), 1.0, 2.0, 0.0)],
        effective_trend=TrendLabel.UP,
    )

    result, next_state = ChannelEngine().update(
        completed_bar(1, 2.0), trend(2.0, None), state, params()
    )

    assert result.pred_high is None
    assert next_state.effective_trend is TrendLabel.UP
    assert next_state.bars[-1].price == pytest.approx(2.0)


def test_channel_retains_only_the_latest_channel_window_bars() -> None:
    engine = ChannelEngine()
    state = ChannelState.empty()
    result = None
    for index, price in enumerate((1.0, 2.0, 3.0, 4.0)):
        result, state = engine.update(
            completed_bar(index, price), trend(price, TrendLabel.UP), state,
            params(channel_window=3),
        )

    assert result is not None
    assert result.channel_stack_length_after == 3
    assert [bar.price for bar in state.bars] == [2.0, 3.0, 4.0]


def test_decision_engine_records_trigger_count_and_resets_after_persist() -> None:
    engine = DecisionEngine()
    first = engine.evaluate(
        Direction.BUY, 95.0, 100.0, 90.0, None, TrendLabel.UP, DecisionState(), params()
    )
    second = engine.evaluate(
        Direction.BUY,
        95.0,
        100.0,
        90.0,
        None,
        TrendLabel.UP,
        first.next_state_after_persist,
        params(),
    )

    assert first.result.decision is DecisionLabel.NO_BUY
    assert first.result.recorded_break_count == 1
    assert second.result.decision is DecisionLabel.BUY
    assert second.result.recorded_break_count == 2
    assert second.result.triggered is True
    assert second.next_state_after_persist.break_count == 0


def test_decision_engine_buy_and_sell_reset_on_boundary_conditions() -> None:
    engine = DecisionEngine()
    buy = engine.evaluate(
        Direction.BUY, 90.0, 100.0, 90.0, None, TrendLabel.UP, DecisionState(4), params()
    )
    sell = engine.evaluate(
        Direction.SELL, 110.0, 100.0, None, 110.0, TrendLabel.DOWN, DecisionState(4), params()
    )

    assert buy.result.decision is DecisionLabel.NO_BUY
    assert sell.result.decision is DecisionLabel.NO_SELL
    assert buy.result.recorded_break_count == 0
    assert sell.result.recorded_break_count == 0
    assert buy.next_state_after_persist.break_count == 0
    assert sell.next_state_after_persist.break_count == 0


@pytest.mark.parametrize(
    ("direction", "effective_trend", "expected"),
    [
        (Direction.BUY, TrendLabel.UP, DecisionLabel.BUY),
        (Direction.BUY, TrendLabel.SIDEWAY, DecisionLabel.BUY),
        (Direction.SELL, TrendLabel.DOWN, DecisionLabel.SELL),
        (Direction.SELL, TrendLabel.SIDEWAY, DecisionLabel.SELL),
    ],
)
def test_decision_engine_allows_directional_effective_trends(
    direction: Direction, effective_trend: TrendLabel, expected: DecisionLabel
) -> None:
    result = DecisionEngine().evaluate(
        direction,
        95.0 if direction is Direction.BUY else 105.0,
        100.0,
        90.0 if direction is Direction.BUY else None,
        110.0 if direction is Direction.SELL else None,
        effective_trend,
        DecisionState(1),
        params(),
    )

    assert result.result.decision is expected
    assert result.result.triggered is True


@pytest.mark.parametrize(
    ("direction", "effective_trend"),
    [
        (Direction.BUY, TrendLabel.DOWN),
        (Direction.BUY, None),
        (Direction.SELL, TrendLabel.UP),
        (Direction.SELL, None),
    ],
)
def test_decision_engine_rejects_opposite_or_missing_effective_trend(
    direction: Direction, effective_trend: TrendLabel | None
) -> None:
    result = DecisionEngine().evaluate(
        direction,
        95.0 if direction is Direction.BUY else 105.0,
        100.0,
        90.0 if direction is Direction.BUY else None,
        110.0 if direction is Direction.SELL else None,
        effective_trend,
        DecisionState(1),
        params(),
    )

    assert result.result.decision is (
        DecisionLabel.NO_BUY if direction is Direction.BUY else DecisionLabel.NO_SELL
    )
    assert result.result.triggered is False
    assert result.next_state_after_persist.break_count == 0
