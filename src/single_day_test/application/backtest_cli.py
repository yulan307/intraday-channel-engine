"""Phase 3 historical backtest CLI with YAML defaults and CLI overrides."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from ..bar_feed.backtest_feed import BacktestFeed
from ..bar_feed.bar_validation import validate_complete_backtest_day
from ..bar_feed.base import BarFeed
from ..domain.enums import Direction, RunMode, ThresholdMode
from ..domain.errors import InputValidationError, NonTradingDayError
from ..domain.models import RunContext, RunSummary
from ..domain.parameters import ParameterSet, load_parameter_sets
from ..domain.states import RuntimeState
from ..ib.config import IbConfig
from ..ib.gateway import IbApiGateway
from ..ib.services import HistoricalBarService, TradingSessionService
from ..persistence.database import Database, SqliteRepositories
from ..support.clock import SystemClock
from ..support.ids import DefaultIdGenerator, IdGenerator
from ..support.logging import JsonLineLogger, StructuredLogger, TerminalMirrorLogger
from .single_day_runner import SingleDayRunner
from .startup_confirmation import print_and_confirm_launch
from .summary_service import build_failed_summary, build_skipped_summary
from .threshold_policy import parse_threshold_update_rate, resolve_config_threshold_mode


DEFAULT_BACKTEST_CONFIG = Path("configs/backtest.yaml")
_ET = ZoneInfo("America/New_York")
_BACKTEST_CONFIG_FIELDS = {
    "symbol", "direction", "threshold", "parameter_set_path", "parameter_set_id",
    "trade_date_start", "trade_date_end", "ib_environment", "database", "ib_config", "threshold_update_rate",
    "log_level",
}


@dataclass(frozen=True)
class BacktestScanRequest:
    symbol: str
    direction: Direction
    trade_dates: tuple[date, ...]
    threshold_mode: ThresholdMode
    fixed_threshold: float | None
    threshold_update_rate: float = 0.0


@dataclass(frozen=True)
class BacktestLaunchConfig:
    request: BacktestScanRequest
    parameter_set_path: Path
    parameter_set_id: str | None
    ib_environment: str
    database: Path
    ib_config: Path
    log_level: str


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
        logger_factory: Callable[[str], StructuredLogger] | None = None,
        gateway_logger_setter: Callable[[StructuredLogger], None] | None = None,
    ) -> None:
        self.database = database
        self.repositories = repositories
        self.id_generator = id_generator
        self.started_at_local = started_at_local
        self.feed_factory = feed_factory
        self.output_dir = output_dir
        self.logger_factory = logger_factory
        self.gateway_logger_setter = gateway_logger_setter

    def execute(
        self, request: BacktestScanRequest, parameter_sets: Sequence[ParameterSet]
    ) -> list[RunSummary]:
        summaries: list[RunSummary] = []
        for params in parameter_sets:
            run_id = self.id_generator.new_run_id(
                self.started_at_local, request.symbol, params.parameter_set_id
            )
            for trade_date in request.trade_dates:
                context = RunContext(
                    run_id, request.symbol, trade_date, params, request.direction,
                    request.threshold_mode, request.fixed_threshold, RunMode.BACKTEST,
                    None, self.started_at_local, threshold_update_rate=getattr(request, "threshold_update_rate", 0.0),
                )
                state = RuntimeState.empty(params, request.fixed_threshold)
                logger = self.logger_factory(run_id) if self.logger_factory is not None else None
                if logger is not None:
                    logger.info("backtest_run_created", run_id=run_id, trade_date=trade_date.isoformat(), parameter_set_id=params.parameter_set_id)
                    if self.gateway_logger_setter is not None:
                        self.gateway_logger_setter(logger)
                runner = SingleDayRunner(self.database, self.repositories, SystemClock(), logger)
                self.repositories.create(context)
                try:
                    feed = self.feed_factory(request.symbol, trade_date)
                except NonTradingDayError as exc:
                    ended = datetime.now().astimezone()
                    self.repositories.mark_skipped(context, str(exc))
                    summary = build_skipped_summary(context, state, ended, str(exc))
                    summaries.append(summary)
                    if logger is not None:
                        logger.summary("run_skipped", run_id=run_id, trade_date=trade_date.isoformat(), reason=str(exc))
                    continue
                except Exception as exc:
                    ended = datetime.now().astimezone()
                    self.repositories.mark_failed(context.run_id, context.trade_date, ended, type(exc).__name__, str(exc))
                    summaries.append(build_failed_summary(context, state, exc, ended))
                    if logger is not None:
                        logger.error("run_failed", run_id=run_id, error_type=type(exc).__name__, error_message=str(exc))
                    continue
                try:
                    summaries.append(runner.execute_run(context, feed, state, create_run=False, write_run_summary=False))
                except Exception:
                    continue
            self.repositories.save_run_summary(run_id)
            self.repositories.export_processed_run_csv(run_id, self.output_dir)
        return summaries


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_BACKTEST_CONFIG)
    parser.add_argument("--symbol")
    parser.add_argument("--direction", choices=("BUY", "SELL"))
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--parameter-set-path", type=Path)
    parser.add_argument("--parameter-set-id")
    parser.add_argument("--trade-date-start", type=date.fromisoformat)
    parser.add_argument("--trade-date-end", type=date.fromisoformat)
    parser.add_argument("--ib-environment", choices=("paper", "live"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--ib-config", type=Path)
    return parser.parse_args()


def load_backtest_config(path: Path) -> dict[str, object]:
    try:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InputValidationError(f"Invalid Backtest config file {path}") from exc
    if not isinstance(document, dict):
        raise InputValidationError(f"Backtest config {path} must be a YAML mapping")
    unknown = set(document) - _BACKTEST_CONFIG_FIELDS
    if unknown:
        raise InputValidationError(f"Backtest config {path} has unsupported fields: {', '.join(sorted(unknown))}")
    return document


def _parse_date(value: object, field: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise InputValidationError(f"{field} must be an ISO date string")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise InputValidationError(f"{field} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InputValidationError(f"{field} must be an ISO date string") from exc


def _parse_dates(start_raw: object, end_raw: object, today_et: date) -> tuple[date, ...]:
    start = _parse_date(start_raw, "trade_date_start")
    end = _parse_date(end_raw, "trade_date_end")
    if start is None and end is None:
        raise InputValidationError("One of trade_date_start or trade_date_end is required")
    start = start or end
    end = end or start
    assert start is not None and end is not None
    if start > end:
        raise InputValidationError("trade_date_start must not be later than trade_date_end")
    if end > today_et:
        raise InputValidationError("Requested trade date must not be later than the current ET date")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def resolve_backtest_launch_config(args: argparse.Namespace, today_et: date) -> BacktestLaunchConfig:
    values = load_backtest_config(args.config)

    def selected(name: str) -> object:
        override = getattr(args, name)
        return override if override is not None else values.get(name)

    symbol = selected("symbol")
    direction = selected("direction")
    threshold = selected("threshold")
    parameter_set_path = selected("parameter_set_path")
    parameter_set_id = selected("parameter_set_id")
    ib_environment = selected("ib_environment")
    database = selected("database")
    ib_config = selected("ib_config")
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
    if threshold is None:
        fixed_threshold = None
    elif isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise InputValidationError("threshold must be numeric, null, or omitted")
    else:
        fixed_threshold = float(threshold)
    threshold_mode = resolve_config_threshold_mode(fixed_threshold, threshold_update_rate_raw is not None)
    if not isinstance(parameter_set_path, (str, Path)) or not str(parameter_set_path):
        raise InputValidationError("parameter_set_path is required")
    if parameter_set_id is not None and not isinstance(parameter_set_id, str):
        raise InputValidationError("parameter_set_id must be a string when supplied")
    if ib_environment not in ("paper", "live"):
        raise InputValidationError("ib_environment must be paper or live")
    if not isinstance(database, (str, Path)) or not str(database):
        raise InputValidationError("database is required")
    if not isinstance(ib_config, (str, Path)) or not str(ib_config):
        raise InputValidationError("ib_config is required")
    if log_level not in {"INFO", "ERROR"}:
        raise InputValidationError("log_level must be INFO or ERROR")
    request = BacktestScanRequest(
        symbol.strip(), parsed_direction,
        _parse_dates(selected("trade_date_start"), selected("trade_date_end"), today_et),
        threshold_mode, fixed_threshold, threshold_update_rate,
    )
    return BacktestLaunchConfig(
        request, Path(parameter_set_path), parameter_set_id.strip() if isinstance(parameter_set_id, str) else None,
        ib_environment, Path(database), Path(ib_config), log_level,
    )


def backtest_launch_configuration(config: BacktestLaunchConfig) -> dict[str, object]:
    return {
        "symbol": config.request.symbol,
        "direction": config.request.direction.value,
        "threshold": config.request.fixed_threshold,
        "threshold_mode": config.request.threshold_mode.value,
        "auto_threshold_enabled": config.request.threshold_mode is ThresholdMode.AUTO,
        "threshold_update_rate": config.request.threshold_update_rate,
        "parameter_set_path": str(config.parameter_set_path),
        "parameter_set_id": config.parameter_set_id,
        "trade_date_start": config.request.trade_dates[0].isoformat(),
        "trade_date_end": config.request.trade_dates[-1].isoformat(),
        "ib_environment": config.ib_environment,
        "database": str(config.database),
        "ib_config": str(config.ib_config),
        "log_level": config.log_level,
    }


def main() -> None:
    args = _args()
    config = resolve_backtest_launch_config(args, datetime.now(_ET).date())
    print_and_confirm_launch("Backtest", backtest_launch_configuration(config))
    config.database.parent.mkdir(parents=True, exist_ok=True)
    parameter_sets = load_parameter_sets(config.parameter_set_path, config.parameter_set_id)
    database = Database(config.database)
    database.initialize()
    repositories = SqliteRepositories(database)
    gateway: IbApiGateway | None = None
    startup_logger = TerminalMirrorLogger(JsonLineLogger(Path("data/logs/startup.jsonl"), SystemClock(), config.log_level))
    active_logger: StructuredLogger = startup_logger

    def set_gateway_logger(logger: StructuredLogger) -> None:
        nonlocal active_logger
        active_logger = logger
        if gateway is not None:
            gateway.set_logger(logger)

    def feed_factory(symbol: str, trade_date: date) -> BacktestFeed:
        nonlocal gateway
        session = repositories.get(trade_date)
        cached = repositories.load_rth_bars(symbol, trade_date) if session is not None else []
        if session is None or not validate_complete_backtest_day(cached, session):
            if gateway is None:
                gateway = IbApiGateway(IbConfig.from_yaml(config.ib_config, config.ib_environment), active_logger)
                gateway.connect_gateway()
            session = TradingSessionService(repositories, gateway).resolve(symbol, trade_date)
            HistoricalBarService(repositories, gateway).load_or_fetch(symbol, session)
        assert session is not None
        return BacktestFeed(symbol, session, repositories)

    try:
        summaries = BacktestScanner(
            database, repositories, DefaultIdGenerator(), datetime.now().astimezone().replace(microsecond=0), feed_factory,
            logger_factory=lambda run_id: TerminalMirrorLogger(JsonLineLogger(Path("data/logs") / f"{run_id}.jsonl", SystemClock(), config.log_level)),
            gateway_logger_setter=set_gateway_logger,
        ).execute(config.request, parameter_sets)
        print(json.dumps([
            {"run_id": item.run_id, "trade_date": item.trade_date.isoformat(), "status": item.status.value,
             "processed_bar_count": item.processed_bar_count, "signal_count": item.signal_count}
            for item in summaries
        ]))
    finally:
        if gateway is not None:
            gateway.disconnect_gateway()
        database.close()


if __name__ == "__main__":
    main()
