"""Phase 4 Live Paper bar-fetch verification CLI; it never submits orders."""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from ..bar_feed.live_paper_feed import LivePaperFeed
from ..domain.enums import Direction
from ..domain.errors import InputValidationError, NonTradingDayError
from ..domain.parameters import load_parameter_sets
from ..ib.config import IbConfig
from ..ib.gateway import IbApiGateway
from ..persistence.database import Database, SqliteRepositories
from ..support.clock import SystemClock


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
    return parser.parse_args()


def resolve_live_session(repositories: SqliteRepositories, gateway: IbApiGateway, symbol: str,
                         start_date: date | None, now_et: datetime):
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
    if args.threshold != args.threshold:
        raise InputValidationError("threshold must be finite")
    # Loading validates that exactly one requested parameter set exists.
    load_parameter_sets(args.parameter_set_path, args.parameter_set_id)
    Direction(args.direction)
    clock = SystemClock()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    database = Database(args.database)
    database.initialize()
    repositories = SqliteRepositories(database)
    gateway = IbApiGateway(IbConfig.from_yaml(args.ib_config, args.ib_environment))
    feed: LivePaperFeed | None = None
    try:
        gateway.connect_gateway()
        now = clock.now_et()
        session = resolve_live_session(repositories, gateway, args.symbol.strip(), args.start_date, now)
        assert session.session_start_et is not None
        delay = (session.session_start_et - now).total_seconds()
        if delay > 0:
            time.sleep(delay)
        feed = LivePaperFeed(args.symbol.strip(), session, gateway, repositories, clock)
        feed.start()
        while True:
            event = feed.next_event()
            if event.bar is not None:
                print(json.dumps({"timestamp": event.bar.raw.timestamp_et.isoformat(), "source": event.bar.source.value}))
                continue
            if event.status.value == "bar_end":
                return
            feed.wait_for_change(1.0)
    finally:
        if feed is not None:
            feed.close()
        gateway.disconnect_gateway()
        database.close()


if __name__ == "__main__":
    main()
