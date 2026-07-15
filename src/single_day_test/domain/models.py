from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .enums import (
    Direction,
    RunMode,
    LivePhase,
    BarSource,
    TrendLabel,
    DecisionLabel,
    RunStatus,
    ThresholdMode,
)
from .parameters import ParameterSet
from .errors import InputValidationError

_ET = ZoneInfo("America/New_York")


def _normalize_dt(dt: datetime) -> datetime:
    """Raise InputValidationError if naive, else convert to America/New_York."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise InputValidationError(
            f"Datetime must be timezone-aware, got naive: {dt}"
        )
    return dt.astimezone(_ET)


def _normalize_optional_dt(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return _normalize_dt(dt)


@dataclass(frozen=True)
class RunRequest:
    symbol: str
    trade_date: date
    parameter_set: ParameterSet
    direction: Direction
    threshold_mode: ThresholdMode
    fixed_threshold: float | None
    threshold_update_rate: float = 0.0


@dataclass(frozen=True)
class RunContext:
    run_id: str
    symbol: str
    trade_date: date
    parameter_set: ParameterSet
    direction: Direction
    threshold_mode: ThresholdMode
    fixed_threshold: float | None
    mode: RunMode
    live_phase: LivePhase | None
    started_at_et: datetime
    threshold_update_rate: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at_et", _normalize_dt(self.started_at_et))


@dataclass(frozen=True)
class RawBar:
    symbol: str
    date: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    wap: float
    barCount: int

    def __post_init__(self) -> None:
        if self.date < 0:
            raise InputValidationError(f"IBAPI bar date must be a UTC epoch second: {self.date}")

    @property
    def timestamp_et(self) -> datetime:
        """Derived ET view; persisted raw data remains the IBAPI epoch value."""
        return datetime.fromtimestamp(self.date, tz=timezone.utc).astimezone(_ET)


@dataclass(frozen=True)
class CompletedBar:
    raw: RawBar
    # Live feeds defer classification until the runner consumes the bar.
    source: BarSource | None


@dataclass(frozen=True)
class TrendBar:
    timestamp_et: datetime
    price: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_et", _normalize_dt(self.timestamp_et))


@dataclass(frozen=True)
class ChannelBar:
    timestamp_et: datetime
    price: float
    high: float
    low: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_et", _normalize_dt(self.timestamp_et))


@dataclass(frozen=True)
class TradingSession:
    trade_date: date
    is_trading_day: bool
    session_start_et: datetime | None
    session_end_et: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_start_et", _normalize_optional_dt(self.session_start_et)
        )
        object.__setattr__(
            self, "session_end_et", _normalize_optional_dt(self.session_end_et)
        )


@dataclass(frozen=True)
class TrendResult:
    price: float
    slope: float | None
    r2: float | None
    slope_rmse: float | None
    slope_std: float | None
    trend_fit_ok: bool | None
    raw_trend: TrendLabel | None
    trend_stack_length_after: int


@dataclass(frozen=True)
class ChannelResult:
    pred_high: float | None
    pred_low: float | None
    effective_trend: TrendLabel | None
    last_trend_slope: float | None
    last_trend_intercept: float | None
    last_trend_bar_count: int | None
    last_high_percentile: float | None
    last_low_percentile: float | None
    curr_trend_slope: float | None
    curr_trend_intercept: float | None
    curr_high_percentile: float | None
    curr_low_percentile: float | None
    channel_stack_length_after: int


@dataclass(frozen=True)
class DecisionResult:
    decision: DecisionLabel
    recorded_break_count: int
    triggered: bool


@dataclass(frozen=True)
class ProcessedBarRecord:
    run_id: str
    symbol: str
    trade_date: date
    timestamp_et: datetime
    mode: RunMode
    bar_source: BarSource
    direction: Direction
    parameter_set_id: str
    parameter_snapshot: dict[str, object]
    active_threshold: float | None
    open: float
    high: float
    low: float
    close: float
    volume: float
    wap: float
    barCount: int
    trend: TrendResult
    channel: ChannelResult
    decision: DecisionResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_et", _normalize_dt(self.timestamp_et))


@dataclass(frozen=True)
class SignalEvent:
    run_id: str
    timestamp_et: datetime
    decision: DecisionLabel
    price: float
    break_count: int
    share: int | None = None
    remained_shares: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp_et", _normalize_dt(self.timestamp_et))


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    symbol: str
    trade_date: date
    mode: RunMode
    direction: Direction
    parameter_set_id: str
    parameter_snapshot: dict[str, object]
    processed_bar_count: int
    signal_count: int
    status: RunStatus
    started_at_et: datetime
    ended_at_et: datetime
    error_type: str | None
    error_message: str | None
    first_threshold: float | None = None
    best_price: float | None = None
    best_order_price: float | None = None
    best_reward: float | None = None
    efficiency: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at_et", _normalize_dt(self.started_at_et))
        object.__setattr__(self, "ended_at_et", _normalize_dt(self.ended_at_et))
