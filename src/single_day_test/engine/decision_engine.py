from __future__ import annotations

from dataclasses import dataclass

from single_day_test.domain.enums import DecisionLabel, Direction
from single_day_test.domain.models import DecisionResult
from single_day_test.domain.parameters import ParameterSet
from single_day_test.domain.states import DecisionState


@dataclass(frozen=True)
class DecisionTransition:
    result: DecisionResult
    next_state_after_persist: DecisionState


class DecisionEngine:
    def evaluate(
        self,
        direction: Direction,
        price: float,
        active_threshold: float,
        pred_high: float | None,
        pred_low: float | None,
        state: DecisionState,
        params: ParameterSet,
    ) -> DecisionTransition:
        if direction is Direction.BUY:
            if (
                pred_high is None
                or price >= active_threshold
                or price <= pred_high
            ):
                return self._no_decision(DecisionLabel.NO_BUY)
            return self._break_transition(DecisionLabel.BUY, state, params)

        if (
            pred_low is None
            or price <= active_threshold
            or price >= pred_low
        ):
            return self._no_decision(DecisionLabel.NO_SELL)
        return self._break_transition(DecisionLabel.SELL, state, params)

    @staticmethod
    def _no_decision(label: DecisionLabel) -> DecisionTransition:
        return DecisionTransition(
            result=DecisionResult(
                decision=label,
                recorded_break_count=0,
                triggered=False,
            ),
            next_state_after_persist=DecisionState(break_count=0),
        )

    @staticmethod
    def _break_transition(
        signal_label: DecisionLabel,
        state: DecisionState,
        params: ParameterSet,
    ) -> DecisionTransition:
        count = state.break_count + 1
        triggered = count >= params.continuous_break_count
        return DecisionTransition(
            result=DecisionResult(
                decision=signal_label if triggered else (
                    DecisionLabel.NO_BUY
                    if signal_label is DecisionLabel.BUY
                    else DecisionLabel.NO_SELL
                ),
                recorded_break_count=count,
                triggered=triggered,
            ),
            next_state_after_persist=DecisionState(
                break_count=0 if triggered else count
            ),
        )
