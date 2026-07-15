from __future__ import annotations

from dataclasses import dataclass
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
        pred_high, pred_low, predicted_count = self._calculate_prediction(state)
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
            price=trend.price,
            high=bar.raw.high,
            low=bar.raw.low,
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
            if old_model is not None:
                next_state.last_trend_slope = old_model.slope
                next_state.last_trend_intercept = old_model.intercept
                next_state.last_high_percentile = old_model.high_percentile
                next_state.last_low_percentile = old_model.low_percentile
                next_state.last_trend_bar_count = 1
            next_state.effective_trend = trend.raw_trend
            next_state.bars = [current_bar]

        if len(next_state.bars) > params.channel_window:
            next_state.bars = next_state.bars[-params.channel_window:]

        current_model = calculate_current_model(
            select_current_model_bars(next_state.bars, params), params
        )
        self._set_current_model(next_state, current_model)
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
            center + state.last_high_percentile,
            center - state.last_low_percentile,
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
