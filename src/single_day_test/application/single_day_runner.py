from __future__ import annotations
from ..bar_feed.base import BarFeed
from ..domain.enums import FeedStatus
from ..domain.models import RunContext, RunSummary
from ..domain.states import RuntimeState
from ..engine.trend_engine import TrendEngine
from ..engine.channel_engine import ChannelEngine
from ..engine.decision_engine import DecisionEngine
from ..persistence.database import Database, SqliteRepositories
from ..support.clock import Clock
from ..support.logging import StructuredLogger
from .bar_processor import process_bar
from .summary_service import build_completed_summary, build_failed_summary

class SingleDayRunner:
    def __init__(self, database: Database, repositories: SqliteRepositories, clock: Clock, logger: StructuredLogger | None = None) -> None:
        self.database, self.repositories, self.clock, self.logger = database, repositories, clock, logger

    def _info(self, event: str, **fields: object) -> None:
        if self.logger is not None:
            self.logger.info(event, **fields)

    def _error(self, event: str, **fields: object) -> None:
        if self.logger is not None:
            self.logger.error(event, **fields)

    def execute_run(self, context: RunContext, feed: BarFeed, initial_state: RuntimeState, *, create_run: bool = True) -> RunSummary:
        state=initial_state
        if create_run:
            self.repositories.create(context)
        feed.start()
        try:
            while True:
                event=feed.next_event()
                if event.status is FeedStatus.BAR_AVAILABLE:
                    assert event.bar is not None
                    transition=process_bar(context,event.bar,state,TrendEngine(),ChannelEngine(),DecisionEngine())
                    with self.database.transaction():
                        self.repositories.insert(transition.record)
                        if transition.signal_event: self.repositories.insert(transition.signal_event)
                    state=transition.next_state_after_persist
                    self._info("bar_processed", run_id=context.run_id, timestamp=event.bar.raw.timestamp_et.isoformat(), source=event.bar.source.value, decision=transition.record.decision.decision.value if transition.record.decision.triggered else None)
                    if transition.signal_event is not None:
                        self._info("signal_triggered", run_id=context.run_id, timestamp=transition.signal_event.timestamp_et.isoformat(), decision=transition.signal_event.decision.value, price=transition.signal_event.price, break_count=transition.signal_event.break_count)
                elif event.status is FeedStatus.BAR_END:
                    summary=build_completed_summary(context,state,self.clock.now_et())
                    self.repositories.complete_with_summary(summary)
                    self._info("run_completed", run_id=context.run_id, processed_bar_count=summary.processed_bar_count, signal_count=summary.signal_count)
                    return summary
                elif event.status is FeedStatus.BAR_WAITING:
                    feed.wait_for_change()
                else: raise RuntimeError(f'Unexpected feed status: {event.status}')
        except Exception as exc:
            summary=build_failed_summary(context,state,exc,self.clock.now_et())
            try:
                self.repositories.fail_with_summary(summary)
            except Exception: pass
            self._error("run_failed", run_id=context.run_id, error_type=type(exc).__name__, error_message=str(exc))
            raise
        finally: feed.close()
