from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque

from .models import TrendBar, ChannelBar, SignalEvent
from .enums import Direction, TrendLabel
from .parameters import ParameterSet


@dataclass
class TrendState:
    bars: deque[TrendBar]
    valid_slopes: deque[float]

    @classmethod
    def empty(cls, params: ParameterSet) -> TrendState:
        return cls(
            bars=deque(maxlen=params.trend_window),
            valid_slopes=deque(maxlen=params.trend_window),
        )


@dataclass
class ChannelState:
    bars: list[ChannelBar] = field(default_factory=list)
    effective_trend: TrendLabel | None = None

    last_trend_slope: float | None = None
    last_trend_intercept: float | None = None
    last_trend_bar_count: int | None = None
    last_high_percentile: float | None = None
    last_low_percentile: float | None = None

    curr_trend_slope: float | None = None
    curr_trend_intercept: float | None = None
    curr_high_percentile: float | None = None
    curr_low_percentile: float | None = None

    @classmethod
    def empty(cls) -> ChannelState:
        return cls()


@dataclass
class DecisionState:
    break_count: int = 0
    opposite_seen: bool = True
    break_trend: TrendLabel | None = None
    trend_changed: bool = True


@dataclass(frozen=True)
class DailyRunStatistics:
    first_threshold: float | None = None
    best_price: float | None = None

    def record(self, active_threshold: float | None, trend_price: float, direction: Direction) -> DailyRunStatistics:
        first_threshold = self.first_threshold if self.first_threshold is not None else active_threshold
        if direction is Direction.BUY:
            best_price = trend_price if self.best_price is None else min(self.best_price, trend_price)
        else:
            best_price = trend_price if self.best_price is None else max(self.best_price, trend_price)
        return DailyRunStatistics(first_threshold, best_price)


@dataclass
class RuntimeState:
    trend: TrendState
    channel: ChannelState
    decision: DecisionState
    active_threshold: float | None = None
    decision_complete: bool = True
    processed_bar_count: int = 0
    signal_events: list[SignalEvent] = field(default_factory=list)
    statistics: DailyRunStatistics = field(default_factory=DailyRunStatistics)

    @classmethod
    def empty(cls, params: ParameterSet, active_threshold: float | None = None) -> RuntimeState:
        return cls(
            trend=TrendState.empty(params),
            channel=ChannelState.empty(),
            decision=DecisionState(),
            active_threshold=active_threshold,
            decision_complete=True,
            processed_bar_count=0,
            signal_events=[],
            statistics=DailyRunStatistics(),
        )
