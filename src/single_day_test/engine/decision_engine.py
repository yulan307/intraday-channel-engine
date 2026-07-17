from __future__ import annotations

from dataclasses import dataclass

from single_day_test.domain.enums import DecisionLabel, Direction, TrendLabel
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
        trend_slope: float | None,
        trend_slope_std: float | None,
        state: DecisionState,
        params: ParameterSet,
        channel_effective_trend: TrendLabel | None = None,
    ) -> DecisionTransition:
        next_state = self._advance_rearm_state(
            direction, state, channel_effective_trend
        )
        if direction is Direction.BUY:
            if not next_state.trend_changed or not next_state.opposite_seen:
                return self._no_decision(DecisionLabel.NO_BUY, next_state)
            if (
                pred_high is None
                or price >= active_threshold
                or price <= pred_high
                or trend_slope is None
                or trend_slope_std is None
                or trend_slope < trend_slope_std
            ):
                return self._no_decision(DecisionLabel.NO_BUY, next_state)
            return self._break_transition(
                DecisionLabel.BUY, next_state, params, channel_effective_trend
            )

        if not next_state.trend_changed or not next_state.opposite_seen:
            return self._no_decision(DecisionLabel.NO_SELL, next_state)
        if (
            pred_low is None
            or price <= active_threshold
            or price >= pred_low
            or trend_slope is None
            or trend_slope_std is None
            or trend_slope > -trend_slope_std
        ):
            return self._no_decision(DecisionLabel.NO_SELL, next_state)
        return self._break_transition(
            DecisionLabel.SELL, next_state, params, channel_effective_trend
        )

    @staticmethod
    def _advance_rearm_state(
        direction: Direction,
        state: DecisionState,
        channel_effective_trend: TrendLabel | None,
    ) -> DecisionState:
        trend_changed = state.trend_changed or (
            channel_effective_trend is not None
            and channel_effective_trend is not state.break_trend
        )
        opposite_seen = state.opposite_seen
        if trend_changed:
            opposite_seen = opposite_seen or (
                direction is Direction.BUY
                and channel_effective_trend is TrendLabel.DOWN
            ) or (
                direction is Direction.SELL
                and channel_effective_trend is TrendLabel.UP
            )
        return DecisionState(
            state.break_count,
            opposite_seen,
            state.break_trend,
            trend_changed,
        )

    @staticmethod
    def _no_decision(
        label: DecisionLabel, state: DecisionState
    ) -> DecisionTransition:
        return DecisionTransition(
            result=DecisionResult(
                decision=label,
                recorded_break_count=0,
                triggered=False,
            ),
            next_state_after_persist=DecisionState(
                break_count=0,
                opposite_seen=state.opposite_seen,
                break_trend=state.break_trend,
                trend_changed=state.trend_changed,
            ),
        )

    @staticmethod
    def _break_transition(
        signal_label: DecisionLabel,
        state: DecisionState,
        params: ParameterSet,
        channel_effective_trend: TrendLabel | None,
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
                break_count=0 if triggered else count,
                opposite_seen=False if triggered else state.opposite_seen,
                break_trend=(
                    channel_effective_trend if triggered else state.break_trend
                ),
                trend_changed=False if triggered else state.trend_changed,
            ),
        )
