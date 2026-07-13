"""Phase 5 Live Paper CLI with YAML defaults and explicit CLI overrides."""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

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


DEFAULT_LIVE_CONFIG = Path("configs/live_config.yaml")
_LIVE_CONFIG_FIELDS = {
    "symbol", "direction", "threshold", "parameter_set_path",
    "parameter_set_id", "ib_environment", "trade_date",
}


@dataclass(frozen=True)
class LiveLaunchConfig:
    symbol: str
    direction: Direction
    threshold: float
    parameter_set_path: Path
    parameter_set_id: str
    ib_environment: str
    trade_date: date | None


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_LIVE_CONFIG)
    parser.add_argument("--symbol")
    parser.add_argument("--direction", choices=("BUY", "SELL"))
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--parameter-set-path", type=Path)
    parser.add_argument("--parameter-set-id")
    parser.add_argument("--trade-date", type=date.fromisoformat)
    parser.add_argument("--database", type=Path, default=Path("data/intraday_channel.sqlite3"))
    parser.add_argument("--ib-config", type=Path, default=Path("configs/ib.yaml"))
    parser.add_argument("--ib-environment", choices=("paper", "live"))
    parser.add_argument("--log-dir", type=Path, default=Path("data/logs"))
    return parser.parse_args()


def load_live_config(path: Path) -> dict[str, object]:
    try:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InputValidationError(f"Invalid Live config file {path}") from exc
    if not isinstance(document, dict):
        raise InputValidationError(f"Live config {path} must be a YAML mapping")
    unknown = set(document) - _LIVE_CONFIG_FIELDS
    if unknown:
        raise InputValidationError(f"Live config {path} has unsupported fields: {', '.join(sorted(unknown))}")
    return document


def resolve_live_launch_config(args: argparse.Namespace) -> LiveLaunchConfig:
    values = load_live_config(args.config)

    def selected(name: str) -> object:
        override = getattr(args, name)
        return override if override is not None else values.get(name)

    symbol = selected("symbol")
    direction = selected("direction")
    threshold = selected("threshold")
    parameter_set_path = selected("parameter_set_path")
    parameter_set_id = selected("parameter_set_id")
    ib_environment = selected("ib_environment")
    trade_date = selected("trade_date")
    if not isinstance(symbol, str) or not symbol.strip():
        raise InputValidationError("symbol is required")
    if not isinstance(direction, str):
        raise InputValidationError("direction is required")
    try:
        parsed_direction = Direction(direction)
    except ValueError as exc:
        raise InputValidationError("direction must be BUY or SELL") from exc
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(float(threshold)):
        raise InputValidationError("threshold must be finite")
    if not isinstance(parameter_set_path, (str, Path)) or not str(parameter_set_path):
        raise InputValidationError("parameter_set_path is required")
    if not isinstance(parameter_set_id, str) or not parameter_set_id.strip():
        raise InputValidationError("parameter_set_id is required")
    if ib_environment not in ("paper", "live"):
        raise InputValidationError("ib_environment must be paper or live")
    if isinstance(trade_date, str):
        try:
            trade_date = date.fromisoformat(trade_date)
        except ValueError as exc:
            raise InputValidationError("trade_date must be YYYY-MM-DD or null") from exc
    if trade_date is not None and not isinstance(trade_date, date):
        raise InputValidationError("trade_date must be YYYY-MM-DD or null")
    return LiveLaunchConfig(
        symbol.strip(), parsed_direction, float(threshold), Path(parameter_set_path),
        parameter_set_id.strip(), ib_environment, trade_date,
    )


def resolve_live_session(repositories: SqliteRepositories, gateway: IbApiGateway, symbol: str,
                         trade_date: date | None, now_et: datetime) -> TradingSession:
    today = now_et.date()
    if trade_date is not None and trade_date < today:
        raise InputValidationError("trade_date must not be earlier than today's ET date")
    first = trade_date or today
    resolved = []
    for offset in range(4):
        candidate = first + timedelta(days=offset)
        session = repositories.get(candidate)
        if session is None:
            session = gateway.query_trading_session(symbol, candidate)
            repositories.save(session)
        resolved.append(session)
    if trade_date is not None:
        session = resolved[0]
        if not session.is_trading_day:
            raise NonTradingDayError(f"{trade_date.isoformat()} is not a trading day")
        if session.session_start_et is None or session.session_end_et is None:
            raise NonTradingDayError(f"{trade_date.isoformat()} has no tradable session")
        if trade_date == today and now_et >= session.session_end_et:
            raise InputValidationError("trade_date is today's completed session")
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
    config = resolve_live_launch_config(args)
    parameter_sets = load_parameter_sets(config.parameter_set_path, config.parameter_set_id)
    if len(parameter_sets) != 1:
        raise InputValidationError("Live Paper requires exactly one parameter set")
    parameter_set = parameter_sets[0]
    clock = SystemClock()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    database = Database(args.database)
    database.initialize()
    repositories = SqliteRepositories(database)
    gateway = IbApiGateway(IbConfig.from_yaml(args.ib_config, config.ib_environment))
    feed: LivePaperFeed | None = None
    context: RunContext | None = None
    state: RuntimeState | None = None
    logger: JsonLineLogger | None = None
    run_created = False
    runner_started = False
    try:
        gateway.connect_gateway()
        now = clock.now_et()
        session = resolve_live_session(repositories, gateway, config.symbol, config.trade_date, now)
        started_at_et = clock.now_et().replace(microsecond=0)
        run_id = DefaultIdGenerator().new_run_id(
            started_at_et.astimezone(), config.symbol, parameter_set.parameter_set_id
        )
        context = RunContext(
            run_id, config.symbol, session.trade_date, parameter_set,
            config.direction, ThresholdMode.FIXED, config.threshold,
            RunMode.LIVE_PAPER, None, started_at_et,
        )
        state = RuntimeState.empty(parameter_set, config.threshold)
        logger = JsonLineLogger(args.log_dir / f"{run_id}.jsonl", clock)
        repositories.create(context)
        run_created = True
        logger.info("run_created", run_id=run_id, symbol=context.symbol, trade_date=context.trade_date.isoformat(), parameter_set_id=parameter_set.parameter_set_id)
        assert session.session_start_et is not None
        delay = (session.session_start_et - clock.now_et()).total_seconds()
        if delay > 0:
            time.sleep(delay)
        feed = LivePaperFeed(config.symbol, session, gateway, repositories, clock)
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
