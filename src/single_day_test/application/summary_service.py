from __future__ import annotations
from dataclasses import asdict
from datetime import datetime
from ..domain.enums import RunStatus
from ..domain.models import RunContext, RunSummary
from ..domain.states import RuntimeState

def _build(context: RunContext, state: RuntimeState, ended: datetime, status: RunStatus, error: Exception | None) -> RunSummary:
    statistics = state.statistics
    if (
        statistics.signal_count == 0
        or statistics.best_price is None
        or statistics.best_order_price is None
        or statistics.best_price == 0
        or statistics.best_order_price == 0
    ):
        best_price = best_order_price = best_reward = efficiency = None
    else:
        best_price = statistics.best_price
        best_order_price = statistics.best_order_price
        best_reward = min(best_price / best_order_price, best_order_price / best_price)
        efficiency = best_reward / statistics.signal_count if best_reward is not None else None
    return RunSummary(context.run_id,context.symbol,context.trade_date,context.mode,context.direction,context.parameter_set.parameter_set_id,asdict(context.parameter_set),state.processed_bar_count,statistics.signal_count,status,context.started_at_et,ended,type(error).__name__ if error else None,str(error) if error else None,statistics.first_threshold,best_price,best_order_price,best_reward,efficiency)
def build_completed_summary(context: RunContext,state: RuntimeState,ended_at_et: datetime) -> RunSummary: return _build(context,state,ended_at_et,RunStatus.COMPLETED,None)
def build_failed_summary(context: RunContext,state: RuntimeState,error: Exception,ended_at_et: datetime) -> RunSummary: return _build(context,state,ended_at_et,RunStatus.FAILED,error)
def build_skipped_summary(context: RunContext, state: RuntimeState, ended_at_et: datetime, reason: str) -> RunSummary:
    return RunSummary(context.run_id,context.symbol,context.trade_date,context.mode,context.direction,context.parameter_set.parameter_set_id,asdict(context.parameter_set),state.processed_bar_count,len(state.signal_events),RunStatus.SKIPPED,context.started_at_et,ended_at_et,"NonTradingDayError",reason)
