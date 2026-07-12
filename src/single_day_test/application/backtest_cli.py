"""Phase 3 multi-parameter, multi-date historical backtest CLI."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..bar_feed.backtest_feed import BacktestFeed
from ..bar_feed.bar_validation import validate_complete_backtest_day
from ..domain.enums import Direction, ThresholdMode
from ..domain.errors import InputValidationError
from ..domain.parameters import load_parameter_sets
from ..ib.config import IbConfig
from ..ib.gateway import IbApiGateway
from ..ib.services import HistoricalBarService, TradingSessionService
from ..persistence.database import Database, SqliteRepositories
from ..support.ids import DefaultIdGenerator
from .backtest_scan import BacktestScanRequest, BacktestScanner

DEFAULT_DATABASE = Path("data/intraday_channel.sqlite3")
DEFAULT_IB_CONFIG = Path("configs/ib.yaml")
_ET = ZoneInfo("America/New_York")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser("init-db")
    init.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    init.add_argument("--rebuild-legacy", action="store_true", help="Compatibility option; incompatible schemas are rebuilt automatically")
    run = subcommands.add_parser("run")
    run.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    run.add_argument("--request", type=Path, required=True, help="JSON: symbol, direction, trade_date_start/end, threshold, parameter_set.path/id")
    run.add_argument("--ib-config", type=Path, default=DEFAULT_IB_CONFIG)
    run.add_argument("--ib-environment", choices=("paper", "live"), default="paper")
    return parser.parse_args()


def _parse_dates(payload: dict[str, object], today_et: date) -> tuple[date, ...]:
    start_raw = payload.get("trade_date_start")
    end_raw = payload.get("trade_date_end")
    if start_raw is None and end_raw is None:
        raise InputValidationError("One of trade_date_start or trade_date_end is required")
    if start_raw is not None and not isinstance(start_raw, str):
        raise InputValidationError("trade_date_start must be an ISO date string")
    if end_raw is not None and not isinstance(end_raw, str):
        raise InputValidationError("trade_date_end must be an ISO date string")
    try:
        start = date.fromisoformat(start_raw) if start_raw is not None else date.fromisoformat(end_raw)  # type: ignore[arg-type]
        end = date.fromisoformat(end_raw) if end_raw is not None else start
    except ValueError as exc:
        raise InputValidationError("trade_date_start and trade_date_end must be ISO dates") from exc
    if start > end:
        raise InputValidationError("trade_date_start must not be later than trade_date_end")
    if end > today_et:
        raise InputValidationError("Requested trade date must not be later than the current ET date")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def parse_scan_request(payload: dict[str, object], today_et: date) -> tuple[BacktestScanRequest, str | None, Path]:
    if "run_id" in payload:
        raise InputValidationError("request JSON must not contain run_id")
    symbol = payload.get("symbol")
    direction = payload.get("direction")
    if not isinstance(symbol, str) or not symbol.strip():
        raise InputValidationError("symbol is required and must be one string")
    if not isinstance(direction, str):
        raise InputValidationError("direction is required")
    try:
        parsed_direction = Direction(direction)
    except ValueError as exc:
        raise InputValidationError("direction must be BUY or SELL") from exc
    threshold = payload.get("threshold")
    if threshold == "":
        raise InputValidationError("threshold must be numeric, null, or omitted; empty string is invalid")
    if threshold is None:
        threshold_mode, fixed_threshold = ThresholdMode.AUTO, None
    elif isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise InputValidationError("threshold must be numeric, null, or omitted")
    else:
        threshold_mode, fixed_threshold = ThresholdMode.FIXED, float(threshold)
    parameter_config = payload.get("parameter_set")
    if not isinstance(parameter_config, dict):
        raise InputValidationError("parameter_set object is required")
    path = parameter_config.get("path")
    selected_id = parameter_config.get("parameter_set_id")
    if not isinstance(path, str) or not path:
        raise InputValidationError("parameter_set.path is required")
    if selected_id is not None and not isinstance(selected_id, str):
        raise InputValidationError("parameter_set.parameter_set_id must be a string when supplied")
    request = BacktestScanRequest(
        symbol.strip(), parsed_direction, _parse_dates(payload, today_et), threshold_mode, fixed_threshold
    )
    return request, selected_id, Path(path)


def main() -> None:
    args = _args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "init-db":
        db = Database(args.database, rebuild_legacy=getattr(args, "rebuild_legacy", False))
        db.initialize()
        print(f"Initialized Phase 3 Expand schema: {args.database}")
        return
    payload = json.loads(args.request.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise InputValidationError("request JSON must be an object")
    started_at_local = datetime.now().astimezone().replace(microsecond=0)
    request, selected_id, parameter_path = parse_scan_request(payload, datetime.now(_ET).date())
    parameter_sets = load_parameter_sets(parameter_path, selected_id)
    db = Database(args.database, rebuild_legacy=getattr(args, "rebuild_legacy", False))
    db.initialize()
    repos = SqliteRepositories(db)
    gateway: IbApiGateway | None = None

    def feed_factory(symbol: str, trade_date: date) -> BacktestFeed:
        nonlocal gateway
        session = repos.get(trade_date)
        cached = repos.load_rth_bars(symbol, trade_date) if session is not None else []
        if session is None or not validate_complete_backtest_day(cached, session):
            if gateway is None:
                gateway = IbApiGateway(IbConfig.from_yaml(args.ib_config, args.ib_environment))
                gateway.connect_gateway()
            session = TradingSessionService(repos, gateway).resolve(symbol, trade_date)
            HistoricalBarService(repos, gateway).load_or_fetch(symbol, session)
        assert session is not None
        return BacktestFeed(symbol, session, repos)

    try:
        summaries = BacktestScanner(
            db, repos, DefaultIdGenerator(), started_at_local, feed_factory
        ).execute(request, parameter_sets)
        print(json.dumps([
            {"run_id": item.run_id, "trade_date": item.trade_date.isoformat(), "status": item.status.value,
             "processed_bar_count": item.processed_bar_count, "signal_count": item.signal_count}
            for item in summaries
        ]))
    finally:
        if gateway is not None:
            gateway.disconnect_gateway()


if __name__ == "__main__":
    main()
