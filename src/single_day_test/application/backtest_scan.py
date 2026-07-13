from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ..bar_feed.base import BarFeed
from ..domain.enums import Direction, RunMode, ThresholdMode
from ..domain.errors import NonTradingDayError
from ..domain.models import RunContext, RunSummary
from ..domain.parameters import ParameterSet
from ..domain.states import RuntimeState
from ..persistence.database import Database, SqliteRepositories
from ..support.clock import SystemClock
from ..support.ids import IdGenerator
from .single_day_runner import SingleDayRunner
from .summary_service import build_failed_summary, build_skipped_summary


@dataclass(frozen=True)
class BacktestScanRequest:
    symbol: str
    direction: Direction
    trade_dates: tuple[date, ...]
    threshold_mode: ThresholdMode
    fixed_threshold: float | None


class BacktestScanner:
    """Run independent daily backtests for each selected parameter set."""

    def __init__(
        self,
        database: Database,
        repositories: SqliteRepositories,
        id_generator: IdGenerator,
        started_at_local: datetime,
        feed_factory: Callable[[str, date], BarFeed],
        output_dir: Path = Path("data"),
    ) -> None:
        self.database = database
        self.repositories = repositories
        self.id_generator = id_generator
        self.started_at_local = started_at_local
        self.feed_factory = feed_factory
        self.output_dir = output_dir

    def execute(
        self, request: BacktestScanRequest, parameter_sets: Sequence[ParameterSet]
    ) -> list[RunSummary]:
        summaries: list[RunSummary] = []
        runner = SingleDayRunner(self.database, self.repositories, SystemClock())
        for params in parameter_sets:
            run_id = self.id_generator.new_run_id(
                self.started_at_local, request.symbol, params.parameter_set_id
            )
            for trade_date in request.trade_dates:
                context = RunContext(
                    run_id, request.symbol, trade_date, params, request.direction,
                    request.threshold_mode, request.fixed_threshold, RunMode.BACKTEST,
                    None, self.started_at_local,
                )
                state = RuntimeState.empty(params, request.fixed_threshold)
                self.repositories.create(context)
                try:
                    feed = self.feed_factory(request.symbol, trade_date)
                except NonTradingDayError as exc:
                    ended = datetime.now().astimezone()
                    self.repositories.mark_skipped(context, str(exc))
                    summary = build_skipped_summary(context, state, ended, str(exc))
                    self.repositories.save_summary(summary)
                    summaries.append(summary)
                    continue
                except Exception as exc:
                    ended = datetime.now().astimezone()
                    self.repositories.mark_failed(context.run_id, context.trade_date, ended, type(exc).__name__, str(exc))
                    summaries.append(build_failed_summary(context, state, exc, ended))
                    self.repositories.save_summary(summaries[-1])
                    continue
                try:
                    summaries.append(runner.execute_run(context, feed, state, create_run=False))
                except Exception:
                    # The daily runner has already stored the FAILED state and partial rows.
                    continue
            self.repositories.export_processed_run_csv(run_id, self.output_dir)
        return summaries
