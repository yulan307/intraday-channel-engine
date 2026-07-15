from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from single_day_test.application import live_cli
from single_day_test.application.live_cli import LiveCliReporter, resolve_live_session, wait_for_session_start, wait_report_interval
from single_day_test.domain.models import TradingSession
from single_day_test.support.logging import JsonLineLogger

ET = ZoneInfo("America/New_York")


@dataclass
class Clock:
    value: datetime

    def now_et(self) -> datetime:
        return self.value


class Repository:
    def __init__(self, sessions: dict[date, TradingSession]) -> None:
        self.sessions = sessions

    def get(self, trade_date: date) -> TradingSession | None:
        return self.sessions.get(trade_date)

    def save(self, session: TradingSession) -> None:
        self.sessions[session.trade_date] = session


class Gateway:
    def __init__(self, session: TradingSession | None = None, logger: object | None = None) -> None:
        self.session = session
        self.connected = False

    def connect_gateway(self) -> None:
        self.connected = True

    def disconnect_gateway(self) -> None:
        self.connected = False

    def set_logger(self, logger: object | None) -> None:
        pass

    def query_trading_session(self, symbol: str, trade_date: date) -> TradingSession:
        assert self.session is not None
        return self.session


def session(trade_date: date, *, start_hour: int = 9, end_hour: int = 16) -> TradingSession:
    return TradingSession(
        trade_date, True,
        datetime(trade_date.year, trade_date.month, trade_date.day, start_hour, 30, tzinfo=ET),
        datetime(trade_date.year, trade_date.month, trade_date.day, end_hour, 0, tzinfo=ET),
    )


def test_session_resolution_reports_explicit_current_and_next_reasons() -> None:
    today = date(2026, 7, 14)
    tomorrow = date(2026, 7, 15)
    current = session(today)
    next_session = session(tomorrow)

    explicit = resolve_live_session(Repository({today: current}), Gateway(current), "AAPL", today, datetime(2026, 7, 14, 9, 0, tzinfo=ET))
    current_result = resolve_live_session(Repository({today: current}), Gateway(current), "AAPL", None, datetime(2026, 7, 14, 9, 0, tzinfo=ET))
    next_result = resolve_live_session(Repository({today: current, tomorrow: next_session}), Gateway(next_session), "AAPL", None, datetime(2026, 7, 14, 16, 0, tzinfo=ET))

    assert explicit.selection_reason == "explicit_trade_date"
    assert current_result.selection_reason == "current_session"
    assert next_result.selection_reason == "next_tradable_session"
    assert next_result.session.trade_date == tomorrow


def test_wait_reporting_intervals_and_fake_clock_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert wait_report_interval(3600.1) == 3600
    assert wait_report_interval(3600) == 900
    assert wait_report_interval(600.1) == 900
    assert wait_report_interval(600) == 60
    assert wait_report_interval(10.1) == 60
    assert wait_report_interval(10) == 1

    clock = Clock(datetime(2026, 7, 14, 8, 9, 59, tzinfo=ET))
    reporter = LiveCliReporter(JsonLineLogger(tmp_path / "run.jsonl", clock))
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.value += timedelta(seconds=seconds)

    wait_for_session_start(clock, datetime(2026, 7, 14, 9, 30, tzinfo=ET), reporter, sleep)

    assert sleeps == [3600, 900, 60, 60, 60, 60, 60, 1]
    assert capsys.readouterr().out.count("WAIT: session_start=") == 8
    events = [json.loads(line) for line in (tmp_path / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [item["event"] for item in events] == ["session_waiting"] * 8
    assert [item["next_status_seconds"] for item in events] == [3600, 900, 60, 60, 60, 60, 60, 1]


def test_main_converts_invalid_yaml_to_logged_exit_code_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("unexpected: value\n", encoding="utf-8")
    args = argparse.Namespace(
        config=config, log_dir=tmp_path / "logs", symbol=None, direction=None,
        threshold=None, parameter_set_path=None, parameter_set_id=None,
        ib_environment=None, trade_date=None,
    )
    monkeypatch.setattr(live_cli, "_args", lambda: args)

    with pytest.raises(SystemExit) as raised:
        live_cli.main()

    assert raised.value.code == 2
    assert capsys.readouterr().out == f"ERROR: Live config {config} has unsupported fields: unexpected\n"
    event = json.loads((tmp_path / "logs" / "startup.jsonl").read_text(encoding="utf-8").strip())
    assert event["event"] == "input_validation_error"


def test_main_converts_invalid_effective_config_to_logged_exit_code_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "invalid-threshold.yaml"
    config.write_text("symbol: AAPL\ndirection: BUY\nthreshold: not-a-number\n", encoding="utf-8")
    args = argparse.Namespace(
        config=config, log_dir=tmp_path / "logs", symbol=None, direction=None,
        threshold=None, parameter_set_path=None, parameter_set_id=None,
        ib_environment=None, trade_date=None,
    )
    monkeypatch.setattr(live_cli, "_args", lambda: args)

    with pytest.raises(SystemExit) as raised:
        live_cli.main()

    assert raised.value.code == 2
    assert capsys.readouterr().out == "ERROR: threshold must be numeric, null, or omitted\n"
    event = json.loads((tmp_path / "logs" / "startup.jsonl").read_text(encoding="utf-8").strip())
    assert event["event"] == "input_validation_error"


def test_main_converts_invalid_requested_date_to_logged_exit_code_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "live.yaml"
    sample_config = yaml.safe_load(
        Path("configs/live_config_sample.yaml").read_text(encoding="utf-8")
    )
    sample_config["parameter_set_path"] = "configs/parameter_set_sample.csv"
    sample_config["trade_date"] = "2000-01-01"
    config.write_text(yaml.safe_dump(sample_config), encoding="utf-8")
    args = argparse.Namespace(
        config=config, log_dir=tmp_path / "logs", database=tmp_path / "run.sqlite3",
        ib_config=Path("configs/ib.yaml"), symbol=None, direction=None,
        threshold=None, parameter_set_path=None, parameter_set_id=None,
        ib_environment=None, trade_date=None,
    )
    monkeypatch.setattr(live_cli, "_args", lambda: args)
    monkeypatch.setattr(live_cli, "IbApiGateway", Gateway)
    monkeypatch.setattr(live_cli, "print_and_confirm_launch", lambda *_args: None)

    with pytest.raises(SystemExit) as raised:
        live_cli.main()

    assert raised.value.code == 2
    assert "ERROR: trade_date must not be earlier than today's ET date" in capsys.readouterr().out
    event = json.loads((tmp_path / "logs" / "startup.jsonl").read_text(encoding="utf-8").strip())
    assert event["event"] == "input_validation_error"
