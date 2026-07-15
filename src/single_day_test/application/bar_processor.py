from __future__ import annotations
from dataclasses import dataclass, asdict
from ..domain.models import CompletedBar, DecisionResult, ProcessedBarRecord, RunContext, SignalEvent
from ..domain.states import ChannelState, DecisionState, RuntimeState, TrendState
from ..engine.trend_engine import TrendEngine
from ..engine.channel_engine import ChannelEngine
from ..engine.decision_engine import DecisionEngine
from .threshold_policy import next_threshold, no_threshold_decision, resolve_threshold
from ..domain.enums import ThresholdMode
from ..domain.errors import InputValidationError

@dataclass(frozen=True)
class BarProcessTransition:
    record: ProcessedBarRecord
    next_state_after_persist: RuntimeState
    signal_event: SignalEvent | None

def process_bar(context: RunContext, bar: CompletedBar, state: RuntimeState, trend_engine: TrendEngine, channel_engine: ChannelEngine, decision_engine: DecisionEngine) -> BarProcessTransition:
    if bar.source is None:
        raise InputValidationError("CompletedBar source must be classified before processing")
    trend, next_trend = trend_engine.update(bar,state.trend,context.parameter_set)
    channel, next_channel = channel_engine.update(bar,trend,state.channel,context.parameter_set)
    active_threshold = resolve_threshold(
        context.threshold_mode, state.active_threshold, state.processed_bar_count, bar.raw.open
    )
    if active_threshold is None:
        decision = no_threshold_decision(context.direction)
        next_decision = DecisionState()
    else:
        decision_transition = decision_engine.evaluate(
            context.direction, trend.price, active_threshold, channel.pred_high, channel.pred_low,
            channel.effective_trend, state.decision, context.parameter_set,
        )
        decision, next_decision = decision_transition.result, decision_transition.next_state_after_persist
    record=ProcessedBarRecord(context.run_id,context.symbol,context.trade_date,bar.raw.timestamp_et,context.mode,bar.source,context.direction,context.parameter_set.parameter_set_id,asdict(context.parameter_set),active_threshold,bar.raw.open,bar.raw.high,bar.raw.low,bar.raw.close,bar.raw.volume,bar.raw.wap,bar.raw.barCount,trend,channel,decision)
    event=SignalEvent(context.run_id,bar.raw.timestamp_et,decision.decision,trend.price,decision.recorded_break_count) if decision.triggered else None
    next_active_threshold = next_threshold(
        context.threshold_mode, active_threshold, trend.price, decision,
        context.direction, context.threshold_update_rate,
    )
    if context.threshold_mode is ThresholdMode.AUTO and decision.triggered:
        next_trend = TrendState.empty(context.parameter_set)
        next_channel = ChannelState.empty()
    statistics = state.statistics.record(active_threshold, trend.price, event.price if event else None, context.direction)
    return BarProcessTransition(record,RuntimeState(next_trend,next_channel,next_decision,next_active_threshold,True,state.processed_bar_count+1,[*state.signal_events,*([event] if event else [])],statistics),event)
