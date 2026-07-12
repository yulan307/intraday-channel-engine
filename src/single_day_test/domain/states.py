from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque

from .models import TrendBar, ChannelBar, SignalEvent
from .enums import TrendLabel
from .parameters import ParameterSet


@dataclass
class TrendState:
    bars: deque[TrendBar]
    valid_slopes: deque[float]

    @classmethod
    def empty(cls, params: ParameterSet) -> TrendState:
        return cls(
            bars=deque(maxlen=params.trend_window),
            valid_slopes=deque(maxlen=params.slope_std_window),
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


@dataclass
class RuntimeState:
    trend: TrendState
    channel: ChannelState
    decision: DecisionState
    decision_complete: bool = True
    processed_bar_count: int = 0
    signal_events: list[SignalEvent] = field(default_factory=list)

    @classmethod
    def empty(cls, params: ParameterSet) -> RuntimeState:
        return cls(
            trend=TrendState.empty(params),
            channel=ChannelState.empty(),
            decision=DecisionState(),
            decision_complete=True,
            processed_bar_count=0,
            signal_events=[],
        )
