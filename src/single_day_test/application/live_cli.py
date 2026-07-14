"""Phase 5 Live Paper CLI with YAML defaults and explicit CLI overrides."""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

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
from ..support.clock import Clock, SystemClock
from ..support.ids import DefaultIdGenerator
from ..support.logging import JsonLineLogger, TerminalMirrorLogger
from .single_day_runner import SingleDayRunner
from .startup_confirmation import print_and_confirm_launch
from .summary_service import build_failed_summary
from .threshold_policy import parse_threshold_update_rate, resolve_config_threshold_mode


DEFAULT_LIVE_CONFIG = Path("configs/live_config.yaml")
_LIVE_CONFIG_FIELDS = {
    "symbol", "direction", "threshold", "parameter_set_path",
    "parameter_set_id", "ib_environment", "trade_date", "threshold_update_rate",
    "log_level",
}


@dataclass(frozen=True)
class LiveLaunchConfig:
    symbol: str
    direction: Direction
    threshold: float | None
    threshold_mode: ThresholdMode
    parameter_set_path: Path
    parameter_set_id: str
    ib_environment: str
    trade_date: date | None
    threshold_update_rate: float = 0.0
    log_level: str = "INFO"


@dataclass(frozen=True)
class LiveSessionResolution:
    session: TradingSession
    requested_trade_date: date | None
    now_et: datetime
    selection_reason: str


@dataclass
class LiveCliReporter:
    logger: JsonLineLogger

    def use_logger(self, logger: JsonLineLogger) -> None:
        self.logger = logger

    def info(self, event: str, message: str, **fields: object) -> None:
        if self.logger.trace_enabled:
            print(message)
        self.logger.info(event, **fields)

    def input_validation_error(self, error: InputValidationError) -> None:
        message = str(error)
        print(f"ERROR: {message}")
        self.logger.error("input_validation_error", error_type=type(error).__name__, error_message=message)


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
    threshold_update_rate_raw = values.get("threshold_update_rate")
    log_level = values.get("log_level")
    threshold_update_rate = parse_threshold_update_rate(threshold_update_rate_raw)
    if not isinstance(symbol, str) or not symbol.strip():
        raise InputValidationError("symbol is required")
    if not isinstance(direction, str):
        raise InputValidationError("direction is required")
    try:
        parsed_direction = Direction(direction)
    except ValueError as exc:
        raise InputValidationError("direction must be BUY or SELL") from exc
    if threshold == "":
        raise InputValidationError("threshold must be numeric, null, or omitted; empty string is invalid")
    if threshold is not None and (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
    ):
        raise InputValidationError("threshold must be numeric, null, or omitted")
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
    if log_level not in {"INFO", "ERROR"}:
        raise InputValidationError("log_level must be INFO or ERROR")
    fixed_threshold = float(threshold) if threshold is not None else None
    return LiveLaunchConfig(
        symbol.strip(), parsed_direction, fixed_threshold,
        resolve_config_threshold_mode(fixed_threshold, threshold_update_rate_raw is not None), Path(parameter_set_path),
        parameter_set_id.strip(), ib_environment, trade_date, threshold_update_rate, log_level,
    )


def live_launch_configuration(config: LiveLaunchConfig) -> dict[str, object]:
    return {
        "symbol": config.symbol,
        "direction": config.direction.value,
        "threshold": config.threshold,
        "threshold_mode": config.threshold_mode.value,
        "auto_threshold_enabled": config.threshold_mode is ThresholdMode.AUTO,
        "threshold_update_rate": config.threshold_update_rate,
        "parameter_set_path": str(config.parameter_set_path),
        "parameter_set_id": config.parameter_set_id,
        "ib_environment": config.ib_environment,
        "trade_date": config.trade_date.isoformat() if config.trade_date else None,
        "log_level": config.log_level,
    }


def resolve_live_session(repositories: SqliteRepositories, gateway: IbApiGateway, symbol: str,
                         trade_date: date | None, now_et: datetime) -> LiveSessionResolution:
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
        return LiveSessionResolution(session, trade_date, now_et, "explicit_trade_date")
    for session in resolved:
        if not session.is_trading_day or session.session_start_et is None or session.session_end_et is None:
            continue
        if session.trade_date == today and now_et >= session.session_end_et:
            continue
        reason = "current_session" if session.trade_date == today else "next_tradable_session"
        return LiveSessionResolution(session, trade_date, now_et, reason)
    raise NonTradingDayError("No tradable session found in the next four calendar dates")


def wait_report_interval(remaining_seconds: float) -> float:
    if remaining_seconds > 60 * 60:
        return 60 * 60
    if remaining_seconds > 10 * 60:
        return 15 * 60
    if remaining_seconds > 10:
        return 60
    return 1


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(math.ceil(seconds)))
    hours, remainder = divmod(total_seconds, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def wait_for_session_start(
    clock: Clock,
    session_start_et: datetime,
    reporter: LiveCliReporter,
    sleep: Callable[[float], None],
) -> None:
    while True:
        now_et = clock.now_et()
        remaining_seconds = (session_start_et - now_et).total_seconds()
        if remaining_seconds <= 0:
            return
        reporting_interval = wait_report_interval(remaining_seconds)
        sleep_seconds = min(reporting_interval, remaining_seconds)
        reporter.info(
            "session_waiting",
            "WAIT: session_start="
            f"{session_start_et.isoformat()} remaining={format_duration(remaining_seconds)} "
            f"next_status_in={format_duration(sleep_seconds)}",
            now_et=now_et.isoformat(),
            session_start_et=session_start_et.isoformat(),
            remaining_seconds=remaining_seconds,
            next_status_seconds=sleep_seconds,
        )
        sleep(sleep_seconds)


def execute_live(
    args: argparse.Namespace,
    clock: Clock,
    reporter: LiveCliReporter,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    config = resolve_live_launch_config(args)
    reporter.use_logger(JsonLineLogger(args.log_dir / "startup.jsonl", clock, config.log_level))
    print_and_confirm_launch("Live", live_launch_configuration(config))
    parameter_sets = load_parameter_sets(config.parameter_set_path, config.parameter_set_id)
    if len(parameter_sets) != 1:
        raise InputValidationError("Live Paper requires exactly one parameter set")
    parameter_set = parameter_sets[0]
    args.database.parent.mkdir(parents=True, exist_ok=True)
    database = Database(args.database)
    database.initialize()
    repositories = SqliteRepositories(database)
    gateway = IbApiGateway(IbConfig.from_yaml(args.ib_config, config.ib_environment), TerminalMirrorLogger(reporter.logger))
    feed: LivePaperFeed | None = None
    context: RunContext | None = None
    state: RuntimeState | None = None
    logger: JsonLineLogger | None = None
    run_created = False
    runner_started = False
    try:
        gateway.connect_gateway()
        now = clock.now_et()
        resolution = resolve_live_session(repositories, gateway, config.symbol, config.trade_date, now)
        session = resolution.session
        started_at_et = clock.now_et().replace(microsecond=0)
        run_id = DefaultIdGenerator().new_run_id(
            started_at_et.astimezone(), config.symbol, parameter_set.parameter_set_id
        )
        context = RunContext(
            run_id, config.symbol, session.trade_date, parameter_set,
            config.direction, config.threshold_mode, config.threshold,
            RunMode.LIVE_PAPER, None, started_at_et, threshold_update_rate=config.threshold_update_rate,
        )
        state = RuntimeState.empty(parameter_set, config.threshold)
        logger = JsonLineLogger(args.log_dir / f"{run_id}.jsonl", clock, config.log_level)
        reporter.use_logger(logger)
        run_logger = TerminalMirrorLogger(logger)
        gateway.set_logger(run_logger)
        repositories.create(context)
        run_created = True
        reporter.info("run_created", f"RUN: created run_id={run_id}", run_id=run_id, symbol=context.symbol, trade_date=context.trade_date.isoformat(), parameter_set_id=parameter_set.parameter_set_id)
        reporter.info(
            "session_resolved",
            "DATE: requested="
            f"{resolution.requested_trade_date.isoformat() if resolution.requested_trade_date else 'null'} "
            f"selected={session.trade_date.isoformat()} reason={resolution.selection_reason} "
            f"session_start={session.session_start_et.isoformat() if session.session_start_et else 'null'} "
            f"session_end={session.session_end_et.isoformat() if session.session_end_et else 'null'}",
            run_id=run_id,
            now_et=resolution.now_et.isoformat(),
            requested_trade_date=resolution.requested_trade_date.isoformat() if resolution.requested_trade_date else None,
            selected_trade_date=session.trade_date.isoformat(),
            selection_reason=resolution.selection_reason,
            session_start_et=session.session_start_et.isoformat() if session.session_start_et else None,
            session_end_et=session.session_end_et.isoformat() if session.session_end_et else None,
        )
        assert session.session_start_et is not None
        wait_for_session_start(clock, session.session_start_et, reporter, sleep)
        def heartbeat(fields: dict[str, object]) -> None:
            print(
                "HEARTBEAT "
                f"run_id={run_id} processed_bar_count={state.processed_bar_count} "
                + " ".join(f"{key}={value}" for key, value in fields.items())
            )

        feed = LivePaperFeed(config.symbol, session, gateway, repositories, clock, heartbeat, run_logger)
        runner_started = True
        summary = SingleDayRunner(database, repositories, clock, run_logger).execute_run(
            context, feed, state, create_run=False, on_first_bar_confirmed=feed.mark_first_bar_confirmed,
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


def main() -> None:
    args = _args()
    clock = SystemClock()
    reporter = LiveCliReporter(JsonLineLogger(args.log_dir / "startup.jsonl", clock))
    try:
        execute_live(args, clock, reporter)
    except InputValidationError as exc:
        reporter.input_validation_error(exc)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
