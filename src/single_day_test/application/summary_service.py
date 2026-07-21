from __future__ import annotations
from dataclasses import asdict
from datetime import datetime
from ..domain.enums import RunMode, RunStatus
from ..domain.metrics import calculate_position_rewards
from ..domain.models import RunContext, RunSummary
from ..domain.states import RuntimeState

def _build(context: RunContext, state: RuntimeState, ended: datetime, status: RunStatus, error: Exception | None) -> RunSummary:
    statistics = state.statistics
    if context.mode is RunMode.BACKTEST:
        first_reward, second_reward, reward = calculate_position_rewards(
            direction=context.direction,
            first_threshold=statistics.first_threshold,
            best_price=statistics.best_price,
            order_prices=tuple(event.price for event in state.signal_events),
        )
    else:
        first_reward = second_reward = reward = None
    return RunSummary(
        context.run_id, context.symbol, context.trade_date, context.mode,
        context.direction, context.parameter_set.parameter_set_id,
        asdict(context.parameter_set), state.processed_bar_count,
        len(state.signal_events), status, context.started_at_et, ended,
        type(error).__name__ if error else None, str(error) if error else None,
        statistics.first_threshold, statistics.best_price,
        first_reward, second_reward, reward,
    )
def build_completed_summary(context: RunContext,state: RuntimeState,ended_at_et: datetime) -> RunSummary: return _build(context,state,ended_at_et,RunStatus.COMPLETED,None)
def build_failed_summary(context: RunContext,state: RuntimeState,error: Exception,ended_at_et: datetime) -> RunSummary: return _build(context,state,ended_at_et,RunStatus.FAILED,error)
def build_skipped_summary(context: RunContext, state: RuntimeState, ended_at_et: datetime, reason: str) -> RunSummary:
    return RunSummary(context.run_id,context.symbol,context.trade_date,context.mode,context.direction,context.parameter_set.parameter_set_id,asdict(context.parameter_set),state.processed_bar_count,len(state.signal_events),RunStatus.SKIPPED,context.started_at_et,ended_at_et,"NonTradingDayError",reason)
