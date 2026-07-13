from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta

from ..domain.enums import BarSource, FeedStatus
from ..domain.errors import BarOrderingError, HistoricalDataError
from ..domain.models import CompletedBar, RawBar, TradingSession
from ..ib.gateway import IbGateway, LiveBarCallbacks, SubscriptionHandle
from ..persistence.raw_bar_repository import RawBarRepository
from ..support.clock import Clock
from .bar_validation import validate_raw_bar
from .base import FeedEvent


class LivePaperFeed:
    """One keep-up-to-date historical request, exposed as ordered completed bars."""

    def __init__(self, symbol: str, session: TradingSession, gateway: IbGateway,
                 raw_bars: RawBarRepository, clock: Clock) -> None:
        if session.session_start_et is None or session.session_end_et is None:
            raise HistoricalDataError("Live session requires start and end timestamps")
        self.symbol, self.session, self.gateway, self.raw_bars, self.clock = symbol, session, gateway, raw_bars, clock
        self._condition = threading.Condition()
        self._hist: dict[datetime, RawBar] = {}
        self._live: dict[datetime, RawBar] = {}
        self._output: deque[CompletedBar] = deque()
        self._in_progress: RawBar | None = None
        self._first_historical_callback = True
        self._historical_done = False
        self._end_bar_emitted = False
        self._closed = False
        self._last_emitted: datetime | None = None
        self._error: Exception | None = None
        self._subscription: SubscriptionHandle | None = None

    def start(self) -> None:
        now = self.clock.now_et()
        start = self.session.session_start_et
        assert start is not None
        seconds = max(1, int((now - start).total_seconds()) + 10)
        self._subscription = self.gateway.start_live_1m_bars(
            self.symbol, seconds,
            LiveBarCallbacks(self._on_historical, self._on_historical_end, self._on_update),
        )

    def _validate(self, bar: RawBar) -> None:
        start, end = self.session.session_start_et, self.session.session_end_et
        assert start is not None and end is not None
        validate_raw_bar(bar)
        if not start <= bar.timestamp_et < end:
            raise HistoricalDataError(f"Live bar outside resolved RTH session: {bar.timestamp_et.isoformat()}")

    def _on_historical(self, bar: RawBar) -> None:
        with self._condition:
            try:
                is_first_callback = self._first_historical_callback
                self._first_historical_callback = False
                start = self.session.session_start_et
                assert start is not None
                # IBKR may prepend the prior RTH session's final bar to the
                # initial keep-up-to-date history response. It is a callback
                # boundary marker, not target-session data.
                if is_first_callback and bar.timestamp_et < start:
                    validate_raw_bar(bar)
                    return
                self._validate(bar)
                # The currently forming bar must wait for update completion.
                if bar.timestamp_et < self.clock.now_et().replace(second=0, microsecond=0):
                    self._hist[bar.timestamp_et] = bar
            except Exception as exc:
                self._error = exc
            self._condition.notify_all()

    def _on_historical_end(self) -> None:
        with self._condition:
            try:
                self._historical_done = True
                self._process_batch([*self._hist.values(), *self._live.values()])
                self._hist.clear(); self._live.clear()
            except Exception as exc:
                self._error = exc
            self._condition.notify_all()

    def _on_update(self, bar: RawBar) -> None:
        with self._condition:
            try:
                self._validate(bar)
                previous = self._in_progress
                if previous is None or previous.timestamp_et == bar.timestamp_et:
                    self._in_progress = bar
                else:
                    self._in_progress = bar
                    if self._historical_done:
                        self._process_batch([previous])
                    else:
                        self._live[previous.timestamp_et] = self._choose(self._live.get(previous.timestamp_et), previous, live=True)
            except Exception as exc:
                self._error = exc
            self._condition.notify_all()

    @staticmethod
    def _choose(existing: RawBar | None, incoming: RawBar, *, live: bool) -> RawBar:
        if existing is None or incoming.volume > existing.volume:
            return incoming
        return incoming if live and incoming.volume == existing.volume else existing

    def _process_batch(self, bars: list[RawBar]) -> None:
        now = self.clock.now_et().replace(second=0, microsecond=0)
        chosen: dict[datetime, tuple[RawBar, bool]] = {}
        for bar in bars:
            previous = chosen.get(bar.timestamp_et)
            if previous is None:
                chosen[bar.timestamp_et] = (bar, bar.timestamp_et in self._live)
                continue
            prior, prior_live = previous
            if bar.volume > prior.volume or (bar.volume == prior.volume and bar.timestamp_et in self._live):
                chosen[bar.timestamp_et] = (bar, bar.timestamp_et in self._live)
        for timestamp in sorted(chosen):
            if self._last_emitted is not None and timestamp <= self._last_emitted:
                raise BarOrderingError(f"Late or duplicate completed live bar: {timestamp.isoformat()}")
            bar, _ = chosen[timestamp]
            end = self.session.session_end_et
            assert end is not None
            source = BarSource.END if timestamp == end - timedelta(minutes=1) and self.clock.now_et() >= end else (BarSource.LIVE if timestamp == now - timedelta(minutes=1) else BarSource.HIST)
            self.raw_bars.upsert_many([bar], bar_size="1 min", what_to_show="TRADES", use_rth=True)
            self._output.append(CompletedBar(bar, source))
            self._last_emitted = timestamp

    def _finalize_end(self) -> None:
        end = self.session.session_end_et
        assert end is not None
        now = self.clock.now_et()
        if now < end:
            return
        expected = end - timedelta(minutes=1)
        if self._in_progress is not None and self._in_progress.timestamp_et == expected:
            self._process_batch([self._in_progress])
            self._in_progress = None
        if self._last_emitted == expected or any(bar.raw.timestamp_et == expected for bar in self._output):
            return
        if now >= end + timedelta(seconds=60):
            raise HistoricalDataError("Timed out waiting for final expected RTH bar")

    def next_event(self) -> FeedEvent:
        with self._condition:
            if self._error is not None:
                raise self._error
            self._finalize_end()
            if self._error is not None:
                raise self._error
            if self._output:
                bar = self._output.popleft()
                if bar.source is BarSource.END:
                    self._end_bar_emitted = True
                return FeedEvent(FeedStatus.BAR_AVAILABLE, bar)
            if self._end_bar_emitted:
                return FeedEvent(FeedStatus.BAR_END, None)
            return FeedEvent(FeedStatus.BAR_WAITING, None)

    def wait_for_change(self, timeout: float | None = None) -> None:
        with self._condition:
            self._condition.wait(self._wait_timeout(timeout))

    def _wait_timeout(self, timeout: float | None) -> float | None:
        if timeout is not None:
            return timeout
        end = self.session.session_end_et
        assert end is not None
        now = self.clock.now_et()
        deadline = end if now < end else end + timedelta(seconds=60)
        return max(0.0, (deadline - now).total_seconds())

    def close(self) -> None:
        with self._condition:
            if not self._closed:
                self._closed = True
                if self._subscription is not None:
                    self._subscription.close()
                self._condition.notify_all()
