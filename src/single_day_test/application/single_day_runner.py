from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import timedelta

from ..bar_feed.base import BarFeed
from ..domain.enums import BarSource, FeedStatus, RunMode
from ..domain.models import CompletedBar, ProcessedBarRecord, RunContext, RunSummary, TradingSession
from ..domain.states import RuntimeState
from ..engine.channel_engine import ChannelEngine
from ..engine.decision_engine import DecisionEngine
from ..engine.trend_engine import TrendEngine
from ..persistence.database import Database, SqliteRepositories
from ..support.clock import Clock
from ..support.logging import StructuredLogger
from .bar_processor import process_bar
from .live_order_submitter import LiveOrderSubmitter
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

    def _summary(self, event: str, **fields: object) -> None:
        if self.logger is not None:
            self.logger.summary(event, **fields)

    def _classify_at_consumption(self, bar: CompletedBar, session: TradingSession | None) -> CompletedBar:
        if bar.source is not None:
            return bar
        if session is None or session.session_end_et is None:
            raise RuntimeError("Live completed bar requires a resolved session for source classification")
        now = self.clock.now_et().replace(second=0, microsecond=0)
        timestamp = bar.raw.timestamp_et
        end = session.session_end_et
        if timestamp == end - timedelta(minutes=1) and self.clock.now_et() >= end:
            source = BarSource.END
        elif timestamp == now - timedelta(minutes=1):
            source = BarSource.LIVE
        else:
            source = BarSource.HIST
        return replace(bar, source=source)

    @staticmethod
    def _raise_before_first_bar(state: RuntimeState, exc: Exception) -> None:
        if state.processed_bar_count == 0:
            raise exc

    def execute_run(
        self,
        context: RunContext,
        feed: BarFeed,
        initial_state: RuntimeState,
        *,
        create_run: bool = True,
        write_run_summary: bool = True,
        processed_record_collector: Callable[[ProcessedBarRecord], None] | None = None,
        on_first_bar_confirmed: Callable[[], None] | None = None,
        session: TradingSession | None = None,
        order_submitter: LiveOrderSubmitter | None = None,
    ) -> RunSummary:
        state = initial_state
        if create_run:
            self.repositories.create(context)
        feed.start()
        try:
            while True:
                try:
                    event = feed.next_event()
                except Exception as exc:
                    self._raise_before_first_bar(state, exc)
                    self._error("nonfatal_feed_error", run_id=context.run_id, error_type=type(exc).__name__, error_message=str(exc))
                    feed.clear_error()
                    feed.wait_for_change()
                    continue
                if event.status is FeedStatus.BAR_AVAILABLE:
                    assert event.bar is not None
                    try:
                        if order_submitter is not None and state.processed_bar_count > 0:
                            order_submitter.recover_after_first_bar()
                        bar = self._classify_at_consumption(event.bar, session)
                        self._info("bar_received", run_id=context.run_id, timestamp=bar.raw.timestamp_et.isoformat(), source=bar.source.value)
                        transition = process_bar(context, bar, state, TrendEngine(), ChannelEngine(), DecisionEngine())
                        self._info(
                            "bar_analysis_completed", run_id=context.run_id, timestamp=bar.raw.timestamp_et.isoformat(),
                            trend=asdict(transition.record.trend), channel=asdict(transition.record.channel),
                            decision=asdict(transition.record.decision),
                        )
                    except Exception as exc:
                        self._raise_before_first_bar(state, exc)
                        self._error("bar_processing_failed", run_id=context.run_id, error_type=type(exc).__name__, error_message=str(exc))
                        continue

                    order_submitted = False
                    if (
                        order_submitter is not None
                        and context.mode is RunMode.LIVE_PAPER
                        and bar.source is BarSource.LIVE
                        and transition.signal_event is not None
                    ):
                        try:
                            order_submitted = order_submitter.submit(
                                context.symbol, context.direction, raise_on_error=state.processed_bar_count == 0,
                            )
                        except Exception as exc:
                            self._raise_before_first_bar(state, exc)
                            self._error("order_submission_failed", run_id=context.run_id, error_type=type(exc).__name__, error_message=str(exc))
                            continue

                    if context.mode is RunMode.BACKTEST:
                        if processed_record_collector is None:
                            raise RuntimeError("Backtest requires an in-memory processed-record collector")
                        try:
                            processed_record_collector(transition.record)
                        except Exception as exc:
                            self._raise_before_first_bar(state, exc)
                            self._error(
                                "backtest_csv_collection_failed", run_id=context.run_id,
                                timestamp=bar.raw.timestamp_et.isoformat(), error_type=type(exc).__name__, error_message=str(exc),
                            )
                            continue
                        if transition.signal_event:
                            try:
                                with self.database.transaction():
                                    self.repositories.insert(transition.signal_event)
                            except Exception as exc:
                                self._error(
                                    "backtest_signal_persistence_failed", run_id=context.run_id,
                                    timestamp=bar.raw.timestamp_et.isoformat(), error_type=type(exc).__name__, error_message=str(exc),
                                )
                    else:
                        try:
                            with self.database.transaction():
                                self.repositories.insert(transition.record)
                                if transition.signal_event:
                                    self.repositories.insert(transition.signal_event)
                        except Exception as exc:
                            self._raise_before_first_bar(state, exc)
                            self._error(
                                "bar_persistence_failed", run_id=context.run_id,
                                timestamp=bar.raw.timestamp_et.isoformat(), order_submitted=order_submitted,
                                error_type=type(exc).__name__, error_message=str(exc),
                            )
                            if order_submitted:
                                state = transition.next_state_after_persist
                            continue

                    state = transition.next_state_after_persist
                    self._info(
                        "bar_persisted", run_id=context.run_id, timestamp=bar.raw.timestamp_et.isoformat(),
                        source=bar.source.value, processed_bar_count=state.processed_bar_count,
                    )
                    if transition.signal_event is not None:
                        self._info(
                            "signal_triggered", run_id=context.run_id,
                            timestamp=transition.signal_event.timestamp_et.isoformat(),
                            decision=transition.signal_event.decision.value, price=transition.signal_event.price,
                            break_count=transition.signal_event.break_count,
                        )
                    if state.processed_bar_count == 1:
                        self._info("first_bar_confirmed", run_id=context.run_id, timestamp=bar.raw.timestamp_et.isoformat(), next_action="continue_run_without_info_trace")
                        if on_first_bar_confirmed is not None:
                            on_first_bar_confirmed()
                        if self.logger is not None:
                            self.logger.stop_info_trace()
                elif event.status is FeedStatus.BAR_END:
                    summary = build_completed_summary(context, state, self.clock.now_et())
                    if write_run_summary:
                        self.repositories.complete_with_summary(summary)
                    else:
                        self.repositories.complete_with_summary(summary, write_run_summary=False)
                    self._summary("run_completed", run_id=context.run_id, processed_bar_count=summary.processed_bar_count, signal_count=summary.signal_count)
                    return summary
                elif event.status is FeedStatus.BAR_WAITING:
                    feed.wait_for_change()
                else:
                    raise RuntimeError(f"Unexpected feed status: {event.status}")
        except Exception as exc:
            summary = build_failed_summary(context, state, exc, self.clock.now_et())
            try:
                if write_run_summary:
                    self.repositories.fail_with_summary(summary)
                else:
                    self.repositories.fail_with_summary(summary, write_run_summary=False)
            except Exception:
                pass
            self._error("run_failed", run_id=context.run_id, error_type=type(exc).__name__, error_message=str(exc))
            raise
        finally:
            feed.close()
