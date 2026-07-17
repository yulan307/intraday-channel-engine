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
from single_day_test.engine.channel_engine import (
    ChannelEngine,
    CurrentChannelModel,
    blend_predictions,
    calculate_frozen_last_prediction,
    calculate_current_prediction,
    calculate_current_model,
    normalized_time_mix,
    select_current_model_bars,
)
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


@pytest.mark.parametrize(
    ("count", "expected_count"),
    [(count, min(count, 5) if count <= 10 else count - 5) for count in range(1, 16)],
)
def test_channel_current_model_uses_delayed_oldest_stack_window(
    count: int, expected_count: int
) -> None:
    bars = [
        ChannelBar(
            datetime(2025, 1, 15, 9, 30, tzinfo=ET) + timedelta(minutes=index),
            float(index),
            float(index + 1),
            float(index - 1),
        )
        for index in range(count)
    ]

    selected = select_current_model_bars(
        bars, params(trend_window=10, channel_window=30)
    )

    assert len(selected) == expected_count
    assert [bar.price for bar in selected] == [
        float(index) for index in range(expected_count)
    ]


def test_channel_current_prediction_uses_delayed_forward_distance() -> None:
    model = CurrentChannelModel(
        slope=2.0,
        intercept=10.0,
        high_percentile=5.0,
        low_percentile=3.0,
    )
    channel_params = params(trend_window=10, channel_window=30)

    assert calculate_current_prediction(model, 2, channel_params) == (None, None)
    assert calculate_current_prediction(model, 3, channel_params) == pytest.approx((15.0, 7.0))
    assert calculate_current_prediction(model, 5, channel_params) == pytest.approx((15.0, 7.0))
    assert calculate_current_prediction(model, 6, channel_params) == pytest.approx((17.0, 9.0))
    assert calculate_current_prediction(model, 10, channel_params) == pytest.approx((25.0, 17.0))
    assert calculate_current_prediction(model, 11, channel_params) == pytest.approx((25.0, 17.0))


def test_channel_frozen_last_prediction_advances_from_current_coordinate() -> None:
    model = CurrentChannelModel(
        slope=2.0,
        intercept=10.0,
        high_percentile=5.0,
        low_percentile=3.0,
    )
    channel_params = params(trend_window=10, channel_window=30)

    assert calculate_frozen_last_prediction(model, 8, channel_params) == pytest.approx(
        (23.0, 15.0, 4)
    )
    assert calculate_frozen_last_prediction(model, 10, channel_params) == pytest.approx(
        (27.0, 19.0, 6)
    )
    assert calculate_frozen_last_prediction(model, 11, channel_params) == pytest.approx(
        (27.0, 19.0, 6)
    )


def test_channel_normalized_time_mix_has_exact_delay_endpoints() -> None:
    channel_params = params(trend_window=10, channel_window=30)

    assert normalized_time_mix(5, channel_params) == 0.0
    assert normalized_time_mix(10, channel_params) == 1.0
    assert 0.0 < normalized_time_mix(7, channel_params) < 1.0


def test_channel_blend_preserves_last_warmup_and_ratio_endpoints() -> None:
    assert blend_predictions(None, None, 12.0, 8.0, 1.0) == (None, None, None)
    assert blend_predictions(10.0, 6.0, None, None, 0.5) == (10.0, 6.0, None)
    assert blend_predictions(10.0, 6.0, 14.0, 2.0, 0.0) == (10.0, 6.0, 0.0)
    assert blend_predictions(10.0, 6.0, 14.0, 2.0, 1.0) == (14.0, 2.0, 1.0)


def test_channel_switch_emits_frozen_last_prediction_and_promotes_old_model() -> None:
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

    assert result.pred_high == pytest.approx(6.0)
    assert result.pred_low == pytest.approx(4.0)
    assert result.last_pred_high == pytest.approx(6.0)
    assert result.last_pred_low == pytest.approx(4.0)
    assert result.effective_trend is TrendLabel.DOWN
    assert result.last_trend_slope == pytest.approx(1.0)
    assert result.last_trend_intercept == pytest.approx(3.0)
    assert result.last_trend_bar_count == 2
    assert result.channel_stack_length_after == 1
    assert [item.price for item in state.bars] == [1.0, 2.0, 3.0]
    assert [item.price for item in next_state.bars] == [4.0]


def test_channel_last_prediction_continues_after_stable_current_model_freezes() -> None:
    channel_params = params(trend_window=10, channel_window=30)
    old_bars = [
        ChannelBar(
            datetime(2025, 1, 15, 9, 30, tzinfo=ET) + timedelta(minutes=index),
            float(index),
            float(index + 1),
            float(index - 1),
        )
        for index in range(11)
    ]
    state = ChannelState(
        bars=old_bars,
        effective_trend=TrendLabel.UP,
        curr_trend_slope=2.0,
        curr_trend_intercept=10.0,
        curr_high_percentile=5.0,
        curr_low_percentile=3.0,
    )
    engine = ChannelEngine()

    switch_result, next_state = engine.update(
        completed_bar(11, 11.0), trend(11.0, TrendLabel.DOWN), state, channel_params
    )
    next_result, _ = engine.update(
        completed_bar(12, 12.0), trend(12.0, TrendLabel.DOWN), next_state, channel_params
    )

    assert switch_result.last_pred_high == pytest.approx(27.0)
    assert switch_result.last_pred_low == pytest.approx(19.0)
    assert switch_result.pred_high == pytest.approx(27.0)
    assert switch_result.pred_low == pytest.approx(19.0)
    assert next_result.last_pred_high == pytest.approx(29.0)
    assert next_result.last_pred_low == pytest.approx(21.0)
    assert next_result.pred_high == pytest.approx(29.0)
    assert next_result.pred_low == pytest.approx(21.0)


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
        Direction.BUY, 95.0, 100.0, 90.0, None, 1.0, 0.5, DecisionState(), params(), TrendLabel.DOWN
    )
    second = engine.evaluate(
        Direction.BUY,
        95.0,
        100.0,
        90.0,
        None,
        1.0,
        0.5,
        first.next_state_after_persist,
        params(),
        TrendLabel.UP,
    )

    assert first.result.decision is DecisionLabel.NO_BUY
    assert first.result.recorded_break_count == 1
    assert second.result.decision is DecisionLabel.BUY
    assert second.result.recorded_break_count == 2
    assert second.result.triggered is True
    assert second.next_state_after_persist.break_count == 0
    assert second.next_state_after_persist.opposite_seen is False
    assert second.next_state_after_persist.break_trend is TrendLabel.UP
    assert second.next_state_after_persist.trend_changed is False


@pytest.mark.parametrize(
    (
        "direction",
        "required_trend",
        "other_trend",
        "price",
        "pred_high",
        "pred_low",
        "slope",
    ),
    [
        (Direction.BUY, TrendLabel.DOWN, TrendLabel.UP, 95.0, 90.0, None, 1.0),
        (Direction.SELL, TrendLabel.UP, TrendLabel.DOWN, 105.0, None, 110.0, -1.0),
    ],
)
def test_decision_rearms_only_after_channel_trend_change_and_opposite_observation(
    direction: Direction,
    required_trend: TrendLabel,
    other_trend: TrendLabel,
    price: float,
    pred_high: float | None,
    pred_low: float | None,
    slope: float,
) -> None:
    engine = DecisionEngine()
    signal_params = params(continuous_break_count=1)
    initial_signal = engine.evaluate(
        direction,
        price,
        100.0,
        pred_high,
        pred_low,
        slope,
        0.5,
        DecisionState(),
        signal_params,
        TrendLabel.SIDEWAY,
    )
    unchanged = engine.evaluate(
        direction,
        price,
        100.0,
        pred_high,
        pred_low,
        slope,
        0.5,
        initial_signal.next_state_after_persist,
        signal_params,
        TrendLabel.SIDEWAY,
    )
    none_trend = engine.evaluate(
        direction,
        price,
        100.0,
        pred_high,
        pred_low,
        slope,
        0.5,
        unchanged.next_state_after_persist,
        signal_params,
        None,
    )
    changed_without_opposite = engine.evaluate(
        direction,
        price,
        100.0,
        pred_high,
        pred_low,
        slope,
        0.5,
        none_trend.next_state_after_persist,
        signal_params,
        other_trend,
    )
    rearmed_signal = engine.evaluate(
        direction,
        price,
        100.0,
        pred_high,
        pred_low,
        slope,
        0.5,
        changed_without_opposite.next_state_after_persist,
        signal_params,
        required_trend,
    )

    assert initial_signal.result.triggered is True
    assert initial_signal.next_state_after_persist.break_trend is TrendLabel.SIDEWAY
    assert unchanged.result.triggered is False
    assert unchanged.next_state_after_persist.trend_changed is False
    assert none_trend.next_state_after_persist.trend_changed is False
    assert changed_without_opposite.result.triggered is False
    assert changed_without_opposite.next_state_after_persist.trend_changed is True
    assert changed_without_opposite.next_state_after_persist.opposite_seen is False
    assert rearmed_signal.result.triggered is True
    assert rearmed_signal.next_state_after_persist.break_trend is required_trend
    assert rearmed_signal.next_state_after_persist.opposite_seen is False
    assert rearmed_signal.next_state_after_persist.trend_changed is False


def test_decision_engine_buy_and_sell_reset_on_boundary_conditions() -> None:
    engine = DecisionEngine()
    buy = engine.evaluate(
        Direction.BUY, 90.0, 100.0, 90.0, None, 1.0, 0.5, DecisionState(4, True), params()
    )
    sell = engine.evaluate(
        Direction.SELL, 110.0, 100.0, None, 110.0, -1.0, 0.5, DecisionState(4, True), params()
    )

    assert buy.result.decision is DecisionLabel.NO_BUY
    assert sell.result.decision is DecisionLabel.NO_SELL
    assert buy.result.recorded_break_count == 0
    assert sell.result.recorded_break_count == 0
    assert buy.next_state_after_persist.break_count == 0
    assert sell.next_state_after_persist.break_count == 0


@pytest.mark.parametrize(
    ("direction", "trend_slope", "trend_slope_std", "expected"),
    [
        (Direction.BUY, 1.0, 0.5, DecisionLabel.BUY),
        (Direction.BUY, 0.5, 0.5, DecisionLabel.BUY),
        (Direction.SELL, -1.0, 0.5, DecisionLabel.SELL),
        (Direction.SELL, -0.5, 0.5, DecisionLabel.SELL),
    ],
)
def test_decision_engine_allows_directional_slope_thresholds(
    direction: Direction, trend_slope: float, trend_slope_std: float, expected: DecisionLabel
) -> None:
    result = DecisionEngine().evaluate(
        direction,
        95.0 if direction is Direction.BUY else 105.0,
        100.0,
        90.0 if direction is Direction.BUY else None,
        110.0 if direction is Direction.SELL else None,
        trend_slope,
        trend_slope_std,
        DecisionState(1, True),
        params(),
    )

    assert result.result.decision is expected
    assert result.result.triggered is True


@pytest.mark.parametrize(
    ("direction", "trend_slope", "trend_slope_std"),
    [
        (Direction.BUY, 0.4, 0.5),
        (Direction.BUY, None, 0.5),
        (Direction.BUY, 1.0, None),
        (Direction.SELL, -0.4, 0.5),
        (Direction.SELL, None, 0.5),
        (Direction.SELL, -1.0, None),
    ],
)
def test_decision_engine_rejects_ineligible_or_missing_slope_values(
    direction: Direction, trend_slope: float | None, trend_slope_std: float | None
) -> None:
    result = DecisionEngine().evaluate(
        direction,
        95.0 if direction is Direction.BUY else 105.0,
        100.0,
        90.0 if direction is Direction.BUY else None,
        110.0 if direction is Direction.SELL else None,
        trend_slope,
        trend_slope_std,
        DecisionState(1, True),
        params(),
    )

    assert result.result.decision is (
        DecisionLabel.NO_BUY if direction is Direction.BUY else DecisionLabel.NO_SELL
    )
    assert result.result.triggered is False
    assert result.next_state_after_persist.break_count == 0
