from __future__ import annotations

from collections import deque

import numpy as np

from single_day_test.domain.enums import TrendLabel
from single_day_test.domain.models import CompletedBar, TrendBar, TrendResult
from single_day_test.domain.parameters import ParameterSet
from single_day_test.domain.states import TrendState

from .regression import linear_regression


def classify_raw_trend(
    slope: float,
    slope_std: float | None,
    r2: float,
    r2_threshold: float,
) -> TrendLabel | None:
    if slope_std is None or r2 < r2_threshold:
        return None
    if slope > slope_std:
        return TrendLabel.UP
    if slope < -slope_std:
        return TrendLabel.DOWN
    return TrendLabel.SIDEWAY


class TrendEngine:
    def update(
        self,
        bar: CompletedBar,
        state: TrendState,
        params: ParameterSet,
    ) -> tuple[TrendResult, TrendState]:
        price = (bar.raw.high + bar.raw.low + bar.raw.close) / 3.0
        next_bars = deque(state.bars, maxlen=params.trend_window)
        next_bars.append(TrendBar(timestamp_et=bar.raw.timestamp_et, price=price))

        if len(next_bars) < 3:
            return (
                TrendResult(
                    price=price,
                    slope=None,
                    r2=None,
                    slope_rmse=None,
                    slope_std=None,
                    trend_fit_ok=None,
                    raw_trend=None,
                    trend_stack_length_after=len(next_bars),
                ),
                TrendState(
                    bars=next_bars,
                    valid_slopes=deque(state.valid_slopes, maxlen=params.slope_std_window),
                ),
            )

        x = np.arange(len(next_bars), dtype=float)
        y = np.array([item.price for item in next_bars], dtype=float)
        regression = linear_regression(x, y)
        next_valid_slopes = deque(state.valid_slopes, maxlen=params.slope_std_window)
        next_valid_slopes.append(regression.slope)
        slope_std = (
            float(np.std(np.array(next_valid_slopes, dtype=float), ddof=0))
            if len(next_valid_slopes) >= 2
            else None
        )
        trend_fit_ok = regression.r2 >= params.r2_threshold
        raw_trend = classify_raw_trend(
            slope=regression.slope,
            slope_std=slope_std,
            r2=regression.r2,
            r2_threshold=params.r2_threshold,
        )
        return (
            TrendResult(
                price=price,
                slope=regression.slope,
                r2=regression.r2,
                slope_rmse=regression.rmse,
                slope_std=slope_std,
                trend_fit_ok=trend_fit_ok,
                raw_trend=raw_trend,
                trend_stack_length_after=len(next_bars),
            ),
            TrendState(bars=next_bars, valid_slopes=next_valid_slopes),
        )
