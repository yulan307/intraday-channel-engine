from __future__ import annotations

from dataclasses import dataclass
import math

from ..domain.enums import DecisionLabel, Direction, ThresholdMode
from ..domain.models import DecisionResult
from ..domain.errors import InputValidationError


@dataclass(frozen=True)
class ThresholdEvaluation:
    active_threshold: float | None
    decision: DecisionResult | None


def resolve_threshold(
    mode: ThresholdMode,
    previous_threshold: float | None,
    processed_bar_count: int,
    opening_price: float,
) -> float | None:
    if mode is ThresholdMode.FIXED:
        return previous_threshold
    if previous_threshold is not None:
        return previous_threshold
    if processed_bar_count == 0:
        return opening_price
    return None


def no_threshold_decision(direction: Direction) -> DecisionResult:
    return DecisionResult(
        DecisionLabel.NO_BUY if direction is Direction.BUY else DecisionLabel.NO_SELL,
        0,
        False,
    )


def next_threshold(
    mode: ThresholdMode,
    active_threshold: float | None,
    price: float,
    decision: DecisionResult,
    direction: Direction,
    update_rate: float,
) -> float | None:
    if mode is ThresholdMode.AUTO and decision.triggered:
        multiplier = 1 - update_rate / 100 if direction is Direction.BUY else 1 + update_rate / 100
        return price * multiplier
    return active_threshold


def parse_threshold_update_rate(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise InputValidationError("threshold_update_rate must be a finite number from 0 to 100")
    rate = float(value)
    if not 0 <= rate <= 100:
        raise InputValidationError("threshold_update_rate must be from 0 to 100")
    return rate
