from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo
from ..bar_feed.base import BarFeed
from ..domain.enums import FeedStatus
from ..domain.models import RunContext, RunSummary
from ..domain.states import RuntimeState
from ..engine.trend_engine import TrendEngine
from ..engine.channel_engine import ChannelEngine
from ..engine.decision_engine import DecisionEngine
from ..persistence.database import Database, SqliteRepositories
from .bar_processor import process_bar
from .summary_service import build_completed_summary, build_failed_summary

class SingleDayRunner:
    def __init__(self, database: Database, repositories: SqliteRepositories) -> None: self.database,self.repositories=database,repositories
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
                elif event.status is FeedStatus.BAR_END:
                    summary=build_completed_summary(context,state,datetime.now(ZoneInfo('America/New_York')))
                    self.repositories.save_summary(summary); self.repositories.mark_completed(context.run_id,context.trade_date,summary.ended_at_et); return summary
                elif event.status is not FeedStatus.BAR_WAITING: raise RuntimeError(f'Unexpected feed status: {event.status}')
        except Exception as exc:
            summary=build_failed_summary(context,state,exc,datetime.now(ZoneInfo('America/New_York')))
            try:
                self.repositories.save_summary(summary); self.repositories.mark_failed(context.run_id,context.trade_date,summary.ended_at_et,type(exc).__name__,str(exc))
            except Exception: pass
            raise
        finally: feed.close()
