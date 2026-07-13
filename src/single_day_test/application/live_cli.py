"""Phase 4 Live Paper bar-fetch verification CLI; it never submits orders."""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from ..bar_feed.live_paper_feed import LivePaperFeed
from ..domain.enums import Direction, RunMode, ThresholdMode
from ..domain.errors import InputValidationError, NonTradingDayError
from ..domain.models import RunContext, TradingSession
from ..domain.parameters import load_parameter_sets
from ..domain.states import RuntimeState
from ..ib.config import IbConfig
from ..ib.gateway import IbApiGateway
from ..persistence.database import Database, SqliteRepositories
from ..support.clock import SystemClock
from ..support.ids import DefaultIdGenerator
from ..support.logging import JsonLineLogger
from .single_day_runner import SingleDayRunner
from .summary_service import build_failed_summary


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--direction", choices=("BUY", "SELL"), required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--parameter-set-path", type=Path, required=True)
    parser.add_argument("--parameter-set-id", required=True)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--database", type=Path, default=Path("data/intraday_channel.sqlite3"))
    parser.add_argument("--ib-config", type=Path, default=Path("configs/ib.yaml"))
    parser.add_argument("--ib-environment", choices=("paper", "live"), default="paper")
    parser.add_argument("--log-dir", type=Path, default=Path("data/logs"))
    return parser.parse_args()


def resolve_live_session(repositories: SqliteRepositories, gateway: IbApiGateway, symbol: str,
                         start_date: date | None, now_et: datetime) -> TradingSession:
    today = now_et.date()
    if start_date is not None and start_date < today:
        raise InputValidationError("start_date must not be earlier than today's ET date")
    first = start_date or today
    resolved = []
    for offset in range(4):
        candidate = first + timedelta(days=offset)
        session = repositories.get(candidate)
        if session is None:
            session = gateway.query_trading_session(symbol, candidate)
            repositories.save(session)
        resolved.append(session)
    if start_date is not None:
        session = resolved[0]
        if not session.is_trading_day:
            raise NonTradingDayError(f"{start_date.isoformat()} is not a trading day")
        if session.session_start_et is None or session.session_end_et is None:
            raise NonTradingDayError(f"{start_date.isoformat()} has no tradable session")
        if start_date == today and now_et >= session.session_end_et:
            raise InputValidationError("start_date is today's completed session")
        return session
    for session in resolved:
        if not session.is_trading_day or session.session_start_et is None or session.session_end_et is None:
            continue
        if session.trade_date == today and now_et >= session.session_end_et:
            continue
        return session
    raise NonTradingDayError("No tradable session found in the next four calendar dates")


def main() -> None:
    args = _args()
    if not args.symbol.strip():
        raise InputValidationError("symbol is required")
    if not math.isfinite(args.threshold):
        raise InputValidationError("threshold must be finite")
    parameter_sets = load_parameter_sets(args.parameter_set_path, args.parameter_set_id)
    if len(parameter_sets) != 1:
        raise InputValidationError("Live Paper requires exactly one parameter set")
    parameter_set = parameter_sets[0]
    Direction(args.direction)
    clock = SystemClock()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    database = Database(args.database)
    database.initialize()
    repositories = SqliteRepositories(database)
    gateway = IbApiGateway(IbConfig.from_yaml(args.ib_config, args.ib_environment))
    feed: LivePaperFeed | None = None
    context: RunContext | None = None
    state: RuntimeState | None = None
    logger: JsonLineLogger | None = None
    run_created = False
    runner_started = False
    try:
        gateway.connect_gateway()
        now = clock.now_et()
        session = resolve_live_session(repositories, gateway, args.symbol.strip(), args.start_date, now)
        started_at_et = clock.now_et().replace(microsecond=0)
        run_id = DefaultIdGenerator().new_run_id(
            started_at_et.astimezone(), args.symbol.strip(), parameter_set.parameter_set_id
        )
        context = RunContext(
            run_id, args.symbol.strip(), session.trade_date, parameter_set,
            Direction(args.direction), ThresholdMode.FIXED, args.threshold,
            RunMode.LIVE_PAPER, None, started_at_et,
        )
        state = RuntimeState.empty(parameter_set, args.threshold)
        logger = JsonLineLogger(args.log_dir / f"{run_id}.jsonl", clock)
        repositories.create(context)
        run_created = True
        logger.info("run_created", run_id=run_id, symbol=context.symbol, trade_date=context.trade_date.isoformat(), parameter_set_id=parameter_set.parameter_set_id)
        assert session.session_start_et is not None
        delay = (session.session_start_et - clock.now_et()).total_seconds()
        if delay > 0:
            time.sleep(delay)
        feed = LivePaperFeed(args.symbol.strip(), session, gateway, repositories, clock)
        runner_started = True
        summary = SingleDayRunner(database, repositories, clock, logger).execute_run(
            context, feed, state, create_run=False,
        )
        print(json.dumps({
            "run_id": summary.run_id,
            "trade_date": summary.trade_date.isoformat(),
            "status": summary.status.value,
            "processed_bar_count": summary.processed_bar_count,
            "signal_count": summary.signal_count,
        }))
    except Exception as exc:
        if run_created and not runner_started and context is not None and state is not None:
            summary = build_failed_summary(context, state, exc, clock.now_et())
            try:
                repositories.fail_with_summary(summary)
            except Exception:
                pass
            if logger is not None:
                logger.error("run_failed", run_id=context.run_id, error_type=type(exc).__name__, error_message=str(exc))
        raise
    finally:
        if feed is not None and not runner_started:
            feed.close()
        gateway.disconnect_gateway()
        database.close()


if __name__ == "__main__":
    main()
