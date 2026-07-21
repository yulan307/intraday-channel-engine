from __future__ import annotations

from dataclasses import dataclass
from math import exp, log
from typing import Sequence

import numpy as np

from single_day_test.domain.models import ChannelBar, ChannelResult, CompletedBar, TrendResult
from single_day_test.domain.parameters import ParameterSet
from single_day_test.domain.states import ChannelState

from .regression import linear_regression


@dataclass(frozen=True)
class CurrentChannelModel:
    slope: float
    intercept: float
    high_percentile: float
    low_percentile: float


def calculate_current_prediction(
    model: CurrentChannelModel | None,
    channel_stack_length: int,
    params: ParameterSet,
) -> tuple[float | None, float | None]:
    """Predict the current bar from the delayed current-model prefix."""
    forward_count = current_prediction_forward_count(channel_stack_length, params)
    if model is None or forward_count is None:
        return None, None
    center = model.slope * forward_count + model.intercept
    return (
        exp(center + model.high_percentile),
        exp(center - model.low_percentile),
    )


def current_prediction_forward_count(
    channel_stack_length: int, params: ParameterSet
) -> int | None:
    """Return the delayed-prefix coordinate for the current bar."""
    delay = params.trend_window // 2
    if channel_stack_length < 3:
        return None
    if channel_stack_length <= delay:
        return 0
    if channel_stack_length <= 2 * delay:
        return channel_stack_length - delay
    return delay


def calculate_frozen_last_prediction(
    model: CurrentChannelModel | None,
    channel_stack_length: int,
    params: ParameterSet,
) -> tuple[float | None, float | None, int | None]:
    """Predict the switch bar from the current model being frozen as last."""
    current_count = current_prediction_forward_count(channel_stack_length, params)
    if model is None or current_count is None:
        return None, None, None
    count = current_count + 1
    center = model.slope * count + model.intercept
    return (
        exp(center + model.high_percentile),
        exp(center - model.low_percentile),
        count,
    )


def normalized_time_mix(channel_stack_length: int, params: ParameterSet) -> float:
    """Return a k=4 sigmoid normalized to exactly span [0, 1]."""
    delay = params.trend_window // 2
    if channel_stack_length <= delay:
        return 0.0
    if channel_stack_length >= 2 * delay:
        return 1.0
    progress = (channel_stack_length - delay) / delay
    steepness = 4.0
    start = 1.0 / (1.0 + exp(steepness / 2.0))
    end = 1.0 / (1.0 + exp(-steepness / 2.0))
    value = 1.0 / (1.0 + exp(-steepness * (progress - 0.5)))
    return (value - start) / (end - start)


def blend_predictions(
    last_pred_high: float | None,
    last_pred_low: float | None,
    curr_pred_high: float | None,
    curr_pred_low: float | None,
    mix: float,
) -> tuple[float | None, float | None, float | None]:
    """Keep first-segment predictions empty and blend only complete pairs."""
    if last_pred_high is None or last_pred_low is None:
        return None, None, None
    if curr_pred_high is None or curr_pred_low is None:
        return last_pred_high, last_pred_low, None
    return (
        last_pred_high * (1.0 - mix) + curr_pred_high * mix,
        last_pred_low * (1.0 - mix) + curr_pred_low * mix,
        mix,
    )


def select_current_model_bars(
    bars: Sequence[ChannelBar], params: ParameterSet
) -> Sequence[ChannelBar]:
    """Select the oldest channel bars used for the current model."""
    count = len(bars)
    delay = params.trend_window // 2
    if count <= delay:
        return bars
    if count <= 2 * delay:
        return bars[:delay]
    return bars[: count - delay]


def is_last_model_ready(state: ChannelState) -> bool:
    return all(
        value is not None
        for value in (
            state.last_trend_slope,
            state.last_trend_intercept,
            state.last_trend_bar_count,
            state.last_high_percentile,
            state.last_low_percentile,
        )
    )


def calculate_current_model(
    bars: Sequence[ChannelBar], params: ParameterSet
) -> CurrentChannelModel | None:
    if len(bars) < 3:
        return None
    x = np.arange(-(len(bars) - 1), 1, dtype=float)
    y = np.array([item.price for item in bars], dtype=float)
    regression = linear_regression(x, y)
    highs = np.array([item.high for item in bars], dtype=float)
    lows = np.array([item.low for item in bars], dtype=float)
    high_deviation = np.abs(highs - regression.predicted)
    low_deviation = np.abs(regression.predicted - lows)
    return CurrentChannelModel(
        slope=regression.slope,
        intercept=regression.intercept,
        high_percentile=float(
            np.percentile(
                high_deviation, params.channel_high_percentile, method="linear"
            )
        ),
        low_percentile=float(
            np.percentile(
                low_deviation, params.channel_low_percentile, method="linear"
            )
        ),
    )


class ChannelEngine:
    def update(
        self,
        bar: CompletedBar,
        trend: TrendResult,
        state: ChannelState,
        params: ParameterSet,
    ) -> tuple[ChannelResult, ChannelState]:
        last_pred_high, last_pred_low, predicted_count = self._calculate_prediction(state)
        next_state = ChannelState(
            bars=list(state.bars),
            effective_trend=state.effective_trend,
            last_trend_slope=state.last_trend_slope,
            last_trend_intercept=state.last_trend_intercept,
            last_trend_bar_count=predicted_count,
            last_high_percentile=state.last_high_percentile,
            last_low_percentile=state.last_low_percentile,
            curr_trend_slope=state.curr_trend_slope,
            curr_trend_intercept=state.curr_trend_intercept,
            curr_high_percentile=state.curr_high_percentile,
            curr_low_percentile=state.curr_low_percentile,
        )
        current_bar = ChannelBar(
            timestamp_et=bar.raw.timestamp_et,
            price=log(trend.price),
            high=log(bar.raw.high),
            low=log(bar.raw.low),
        )

        if not next_state.bars:
            if trend.raw_trend is not None:
                next_state.effective_trend = trend.raw_trend
                next_state.bars = [current_bar]
            else:
                next_state.effective_trend = None
                next_state.bars = []
        elif trend.raw_trend is None or trend.raw_trend == next_state.effective_trend:
            next_state.bars.append(current_bar)
        else:
            old_model = self._current_model_from_state(state)
            (
                frozen_last_pred_high,
                frozen_last_pred_low,
                frozen_last_count,
            ) = calculate_frozen_last_prediction(old_model, len(state.bars), params)
            if frozen_last_count is not None:
                next_state.last_trend_slope = old_model.slope
                next_state.last_trend_intercept = old_model.intercept
                next_state.last_high_percentile = old_model.high_percentile
                next_state.last_low_percentile = old_model.low_percentile
                next_state.last_trend_bar_count = frozen_last_count
                last_pred_high = frozen_last_pred_high
                last_pred_low = frozen_last_pred_low
            next_state.effective_trend = trend.raw_trend
            next_state.bars = [current_bar]

        if len(next_state.bars) > params.channel_window:
            next_state.bars = next_state.bars[-params.channel_window:]

        current_model = calculate_current_model(
            select_current_model_bars(next_state.bars, params), params
        )
        self._set_current_model(next_state, current_model)
        curr_pred_high, curr_pred_low = calculate_current_prediction(
            current_model, len(next_state.bars), params
        )
        pred_high, pred_low, mix = blend_predictions(
            last_pred_high,
            last_pred_low,
            curr_pred_high,
            curr_pred_low,
            normalized_time_mix(len(next_state.bars), params) * params.curr_mix_ratio,
        )
        return (
            ChannelResult(
                pred_high=pred_high,
                pred_low=pred_low,
                effective_trend=next_state.effective_trend,
                last_trend_slope=next_state.last_trend_slope,
                last_trend_intercept=next_state.last_trend_intercept,
                last_trend_bar_count=next_state.last_trend_bar_count,
                last_high_percentile=next_state.last_high_percentile,
                last_low_percentile=next_state.last_low_percentile,
                curr_trend_slope=next_state.curr_trend_slope,
                curr_trend_intercept=next_state.curr_trend_intercept,
                curr_high_percentile=next_state.curr_high_percentile,
                curr_low_percentile=next_state.curr_low_percentile,
                channel_stack_length_after=len(next_state.bars),
                last_pred_high=last_pred_high,
                last_pred_low=last_pred_low,
                curr_pred_high=curr_pred_high,
                curr_pred_low=curr_pred_low,
                mix=mix,
            ),
            next_state,
        )

    @staticmethod
    def _calculate_prediction(
        state: ChannelState,
    ) -> tuple[float | None, float | None, int | None]:
        if not is_last_model_ready(state):
            return None, None, state.last_trend_bar_count
        assert state.last_trend_bar_count is not None
        assert state.last_trend_slope is not None
        assert state.last_trend_intercept is not None
        assert state.last_high_percentile is not None
        assert state.last_low_percentile is not None
        count = state.last_trend_bar_count + 1
        center = state.last_trend_slope * count + state.last_trend_intercept
        return (
            exp(center + state.last_high_percentile),
            exp(center - state.last_low_percentile),
            count,
        )

    @staticmethod
    def _current_model_from_state(state: ChannelState) -> CurrentChannelModel | None:
        values = (
            state.curr_trend_slope,
            state.curr_trend_intercept,
            state.curr_high_percentile,
            state.curr_low_percentile,
        )
        if any(value is None for value in values):
            return None
        assert state.curr_trend_slope is not None
        assert state.curr_trend_intercept is not None
        assert state.curr_high_percentile is not None
        assert state.curr_low_percentile is not None
        return CurrentChannelModel(
            slope=state.curr_trend_slope,
            intercept=state.curr_trend_intercept,
            high_percentile=state.curr_high_percentile,
            low_percentile=state.curr_low_percentile,
        )

    @staticmethod
    def _set_current_model(
        state: ChannelState, model: CurrentChannelModel | None
    ) -> None:
        if model is None:
            state.curr_trend_slope = None
            state.curr_trend_intercept = None
            state.curr_high_percentile = None
            state.curr_low_percentile = None
            return
        state.curr_trend_slope = model.slope
        state.curr_trend_intercept = model.intercept
        state.curr_high_percentile = model.high_percentile
        state.curr_low_percentile = model.low_percentile
