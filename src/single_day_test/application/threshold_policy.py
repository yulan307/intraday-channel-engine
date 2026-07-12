from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import DecisionLabel, Direction, ThresholdMode
from ..domain.models import DecisionResult
from ..domain.parameters import ParameterSet


@dataclass(frozen=True)
class ThresholdEvaluation:
    active_threshold: float | None
    decision: DecisionResult | None


def resolve_threshold(
    mode: ThresholdMode,
    previous_threshold: float | None,
    processed_bar_count: int,
    price: float,
    params: ParameterSet,
) -> float | None:
    if mode is ThresholdMode.FIXED:
        return previous_threshold
    if previous_threshold is not None:
        return previous_threshold
    if processed_bar_count + 1 == params.trend_window:
        return price
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
) -> float | None:
    if mode is ThresholdMode.AUTO and decision.triggered:
        return price
    return active_threshold
