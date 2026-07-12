from __future__ import annotations
from dataclasses import dataclass, asdict
from ..domain.models import CompletedBar, ProcessedBarRecord, RunContext, SignalEvent
from ..domain.states import RuntimeState
from ..engine.trend_engine import TrendEngine
from ..engine.channel_engine import ChannelEngine
from ..engine.decision_engine import DecisionEngine

@dataclass(frozen=True)
class BarProcessTransition:
    record: ProcessedBarRecord
    next_state_after_persist: RuntimeState
    signal_event: SignalEvent | None

def process_bar(context: RunContext, bar: CompletedBar, state: RuntimeState, trend_engine: TrendEngine, channel_engine: ChannelEngine, decision_engine: DecisionEngine) -> BarProcessTransition:
    trend, next_trend = trend_engine.update(bar,state.trend,context.parameter_set)
    channel, next_channel = channel_engine.update(bar,trend,state.channel,context.parameter_set)
    transition=decision_engine.evaluate(context.direction,trend.price,context.active_threshold,channel.pred_high,channel.pred_low,state.decision,context.parameter_set)
    record=ProcessedBarRecord(context.run_id,context.symbol,context.trade_date,bar.raw.timestamp_et,context.mode,bar.source,context.direction,context.parameter_set.parameter_set_id,asdict(context.parameter_set),context.initial_threshold,context.active_threshold,bar.raw.open,bar.raw.high,bar.raw.low,bar.raw.close,bar.raw.volume,bar.raw.wap,bar.raw.barCount,trend,channel,transition.result)
    event=SignalEvent(context.run_id,bar.raw.timestamp_et,transition.result.decision,trend.price,transition.result.recorded_break_count) if transition.result.triggered else None
    return BarProcessTransition(record,RuntimeState(next_trend,next_channel,transition.next_state_after_persist,True,state.processed_bar_count+1,[*state.signal_events,*([event] if event else [])]),event)
