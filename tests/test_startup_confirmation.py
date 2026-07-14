from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from single_day_test.application.startup_confirmation import print_and_confirm_launch
from single_day_test.support.logging import JsonLineLogger


def test_prints_effective_configuration_before_waiting(capsys) -> None:
    prompts: list[str] = []

    print_and_confirm_launch(
        "Backtest",
        {"threshold": None, "threshold_mode": "AUTO", "symbol": "AAPL"},
        prompts.append,
    )

    assert capsys.readouterr().out == (
        "Backtest launch configuration:\n"
        "{\n"
        "  \"symbol\": \"AAPL\",\n"
        "  \"threshold\": null,\n"
        "  \"threshold_mode\": \"AUTO\"\n"
        "}\n"
    )
    assert prompts == ["Press Enter to start, or Ctrl+C to cancel: "]


@dataclass
class _Clock:
    value: datetime

    def now_et(self) -> datetime:
        return self.value


def test_json_logger_defers_directory_creation_until_an_event(tmp_path) -> None:
    path = tmp_path / "logs" / "startup.jsonl"
    logger = JsonLineLogger(path, _Clock(datetime(2026, 7, 14, tzinfo=ZoneInfo("America/New_York"))))

    assert not path.parent.exists()

    logger.info("confirmed")

    assert path.exists()
