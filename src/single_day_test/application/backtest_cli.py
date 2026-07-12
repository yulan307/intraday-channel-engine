"""Phase 3 historical-IBAPI runner; request files contain strategy parameters only."""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import date, datetime
from pathlib import Path

from ..bar_feed.backtest_feed import BacktestFeed
from ..domain.enums import Direction, RunMode
from ..domain.models import RunContext
from ..domain.parameters import load_parameter_set
from ..domain.states import RuntimeState
from ..ib.config import IbConfig
from ..ib.gateway import IbApiGateway
from ..ib.services import HistoricalBarService, TradingSessionService
from ..persistence.database import Database, SqliteRepositories
from .single_day_runner import SingleDayRunner

DEFAULT_DATABASE = Path("data/intraday_channel.sqlite3")
DEFAULT_IB_CONFIG = Path("configs/ib.yaml")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser("init-db")
    init.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    init.add_argument("--rebuild-legacy", action="store_true", help="One-time destructive Phase 2 schema rebuild")
    run = subcommands.add_parser("run")
    run.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    run.add_argument("--request", type=Path, required=True, help="JSON: symbol, trade_date, direction, initial_threshold, parameter_set.path, parameter_set.parameter_set_id")
    run.add_argument("--ib-config", type=Path, default=DEFAULT_IB_CONFIG)
    run.add_argument("--ib-environment", choices=("paper", "live"), default="paper")
    return parser.parse_args()


def main() -> None:
    args = _args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    db = Database(args.database, rebuild_legacy=getattr(args, "rebuild_legacy", False))
    db.initialize()
    if args.command == "init-db":
        print(f"Initialized Phase 3 IBAPI schema: {args.database}")
        return
    payload = json.loads(args.request.read_text(encoding="utf-8"))
    parameter_config = payload["parameter_set"]
    params = load_parameter_set(parameter_config["path"], parameter_config["parameter_set_id"])
    repos = SqliteRepositories(db)
    trade_date = date.fromisoformat(payload["trade_date"])
    symbol = payload["symbol"]
    session = repos.get(trade_date)
    cached = repos.load_rth_bars(symbol, trade_date) if session is not None else []
    gateway: IbApiGateway | None = None
    try:
        from ..bar_feed.bar_validation import validate_complete_backtest_day
        if session is None or not validate_complete_backtest_day(cached, session):
            gateway = IbApiGateway(IbConfig.from_yaml(args.ib_config, args.ib_environment))
            gateway.connect_gateway()
            session = TradingSessionService(repos, gateway).resolve(symbol, trade_date)
            HistoricalBarService(repos, gateway).load_or_fetch(symbol, session)
        assert session is not None
        context = RunContext(payload.get("run_id", str(uuid.uuid4())), symbol, trade_date, params,
            Direction(payload["direction"]), float(payload["initial_threshold"]),
            float(payload.get("active_threshold", payload["initial_threshold"])), RunMode.BACKTEST, None,
            datetime.now().astimezone())
        summary = SingleDayRunner(db, repos).execute_run(context, BacktestFeed(symbol, session, repos), RuntimeState.empty(params))
        print(json.dumps({"run_id": summary.run_id, "status": summary.status.value, "processed_bar_count": summary.processed_bar_count, "signal_count": summary.signal_count}))
    finally:
        if gateway is not None:
            gateway.disconnect_gateway()


if __name__ == "__main__":
    main()
