from dataclasses import dataclass
from .errors import InputValidationError


@dataclass(frozen=True)
class ParameterSet:
    parameter_set_id: str
    trend_window: int
    slope_std_window: int
    dev_window: int
    residual_window: int
    r2_threshold: float
    channel_high_percentile: float
    channel_low_percentile: float
    continuous_break_count: int


def validate_parameter_set(params: ParameterSet) -> None:
    if params.trend_window < 3:
        raise InputValidationError(
            f"trend_window must be >= 3, got {params.trend_window}"
        )
    if params.slope_std_window < 2:
        raise InputValidationError(
            f"slope_std_window must be >= 2, got {params.slope_std_window}"
        )
    if not (0.0 <= params.r2_threshold <= 1.0):
        raise InputValidationError(
            f"r2_threshold must be between 0 and 1 inclusive, got {params.r2_threshold}"
        )
    if not (0.0 <= params.channel_high_percentile <= 100.0):
        raise InputValidationError(
            f"channel_high_percentile must be between 0 and 100 inclusive, "
            f"got {params.channel_high_percentile}"
        )
    if not (0.0 <= params.channel_low_percentile <= 100.0):
        raise InputValidationError(
            f"channel_low_percentile must be between 0 and 100 inclusive, "
            f"got {params.channel_low_percentile}"
        )
    if params.continuous_break_count < 1:
        raise InputValidationError(
            f"continuous_break_count must be >= 1, got {params.continuous_break_count}"
        )
