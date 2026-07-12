from dataclasses import dataclass
import csv
from pathlib import Path
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
    is_active: int = 1


def load_parameter_sets(path: str | Path, parameter_set_id: str | None = None) -> list[ParameterSet]:
    """Load active rows, or one explicitly selected row, from the central CSV."""
    required = {
        "parameter_set_id", "trend_window", "slope_std_window", "dev_window",
        "residual_window", "r2_threshold", "channel_high_percentile",
        "channel_low_percentile", "continuous_break_count", "is_active",
    }
    try:
        with Path(path).open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise InputValidationError(f"Parameter CSV is missing required columns: {sorted(required)}")
            rows = list(reader)
        requested_id = parameter_set_id.strip() if parameter_set_id is not None else ""
        matches = [row for row in rows if row.get("parameter_set_id") == requested_id] if requested_id else [row for row in rows if row.get("is_active") == "1"]
        if requested_id and len(matches) != 1:
            raise InputValidationError(f"Expected exactly one parameter_set_id={requested_id!r} in {path}, found {len(matches)}")
        if not matches:
            raise InputValidationError("No parameter sets selected")
        params_list = []
        for row in matches:
            params = ParameterSet(
                parameter_set_id=row["parameter_set_id"], trend_window=int(row["trend_window"]),
                slope_std_window=int(row["slope_std_window"]), dev_window=int(row["dev_window"]),
                residual_window=int(row["residual_window"]), r2_threshold=float(row["r2_threshold"]),
                channel_high_percentile=float(row["channel_high_percentile"]),
                channel_low_percentile=float(row["channel_low_percentile"]),
                continuous_break_count=int(row["continuous_break_count"]), is_active=int(row["is_active"]),
            )
            validate_parameter_set(params)
            params_list.append(params)
        return params_list
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise InputValidationError(f"Invalid parameter CSV {path} for parameter_set_id={parameter_set_id!r}") from exc


def load_parameter_set(path: str | Path, parameter_set_id: str) -> ParameterSet:
    """Backward-compatible single-row selection."""
    return load_parameter_sets(path, parameter_set_id)[0]


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
