from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from single_day_test.domain.errors import InputValidationError


@dataclass(frozen=True)
class RegressionResult:
    slope: float
    intercept: float
    r2: float
    rmse: float
    predicted: np.ndarray


def linear_regression(x: np.ndarray, y: np.ndarray) -> RegressionResult:
    """Fit a finite, one-dimensional ordinary least-squares line."""
    x_values = np.asarray(x, dtype=float)
    y_values = np.asarray(y, dtype=float)

    if x_values.ndim != 1 or y_values.ndim != 1:
        raise InputValidationError("x and y must be one-dimensional arrays")
    if len(x_values) != len(y_values):
        raise InputValidationError("x and y must have the same length")
    if len(x_values) < 2:
        raise InputValidationError("linear regression requires at least two points")
    if not np.isfinite(x_values).all() or not np.isfinite(y_values).all():
        raise InputValidationError("x and y must not contain NaN or infinity")

    x_centered = x_values - float(np.mean(x_values))
    denominator = float(np.dot(x_centered, x_centered))
    if denominator == 0.0:
        raise InputValidationError("x values must not all be identical")

    y_mean = float(np.mean(y_values))
    slope = float(np.dot(x_centered, y_values - y_mean) / denominator)
    intercept = y_mean - slope * float(np.mean(x_values))
    predicted = slope * x_values + intercept
    residuals = y_values - predicted
    rmse = float(np.sqrt(np.mean(residuals**2)))
    total_sum_squares = float(np.sum((y_values - y_mean) ** 2))
    r2 = 1.0 if total_sum_squares == 0.0 else float(
        1.0 - np.sum(residuals**2) / total_sum_squares
    )

    return RegressionResult(
        slope=slope,
        intercept=float(intercept),
        r2=r2,
        rmse=rmse,
        predicted=predicted,
    )
