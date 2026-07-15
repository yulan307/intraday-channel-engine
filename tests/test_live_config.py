from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pytest

from single_day_test.application.live_cli import live_launch_configuration, resolve_live_launch_config
from single_day_test.domain.enums import Direction, ThresholdMode
from single_day_test.domain.errors import InputValidationError


def args(config: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "config": config, "symbol": None, "direction": None, "threshold": None,
        "parameter_set_path": None, "parameter_set_id": None,
        "ib_environment": None, "trade_date": None,
        "shares": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_yaml_launch_defaults_and_cli_overrides(tmp_path: Path) -> None:
    path = tmp_path / "live.yaml"
    path.write_text(
        "symbol: AAPL\ndirection: BUY\nthreshold: 100\nparameter_set_path: configs/parameter_set.csv\nparameter_set_id: p1\nib_environment: paper\nshares: [1, 2]\ntrade_date: 2026-07-15\nlog_level: INFO\n",
        encoding="utf-8",
    )

    configured = resolve_live_launch_config(args(path))
    overridden = resolve_live_launch_config(args(
        path, symbol="MSFT", direction="SELL", threshold=150.0,
        trade_date=date(2026, 7, 16), ib_environment="live",
    ))

    assert configured.symbol == "AAPL"
    assert configured.direction is Direction.BUY
    assert configured.trade_date == date(2026, 7, 15)
    assert configured.threshold_update_rate == 0.0
    assert configured.threshold_mode is ThresholdMode.FIXED
    assert configured.shares == (1, 2)
    assert live_launch_configuration(configured)["auto_threshold_enabled"] is False
    assert overridden.symbol == "MSFT"
    assert overridden.direction is Direction.SELL
    assert overridden.threshold == 150.0
    assert overridden.threshold_mode is ThresholdMode.FIXED
    assert overridden.ib_environment == "live"
    assert overridden.trade_date == date(2026, 7, 16)


def test_live_config_rejects_missing_or_unsupported_values(tmp_path: Path) -> None:
    path = tmp_path / "live.yaml"
    path.write_text("symbol: AAPL\nunknown: value\n", encoding="utf-8")

    with pytest.raises(InputValidationError, match="unsupported fields"):
        resolve_live_launch_config(args(path))

    path.write_text(
        "symbol: AAPL\ndirection: BUY\nthreshold: 100\nparameter_set_path: params.csv\nparameter_set_id: p1\nib_environment: paper\nlog_level: DEBUG\n",
        encoding="utf-8",
    )
    with pytest.raises(InputValidationError, match="log_level"):
        resolve_live_launch_config(args(path))


def test_live_config_allows_null_threshold_as_auto_mode(tmp_path: Path) -> None:
    path = tmp_path / "live.yaml"
    path.write_text(
        "symbol: AAPL\ndirection: BUY\nthreshold: null\nthreshold_update_rate: 12.5\nparameter_set_path: params.csv\nparameter_set_id: p1\nib_environment: paper\nshares: [1]\ntrade_date: null\nlog_level: INFO\n",
        encoding="utf-8",
    )

    configured = resolve_live_launch_config(args(path))

    assert configured.threshold is None
    assert configured.threshold_update_rate == 12.5
    assert live_launch_configuration(configured)["threshold_mode"] is ThresholdMode.AUTO.value
    assert live_launch_configuration(configured)["auto_threshold_enabled"] is True
    assert live_launch_configuration(configured)["threshold_update_rate"] == 12.5


def test_live_config_allows_null_threshold_update_rate_as_zero(tmp_path: Path) -> None:
    path = tmp_path / "live.yaml"
    path.write_text(
        "symbol: AAPL\ndirection: BUY\nthreshold: null\nthreshold_update_rate:\nparameter_set_path: params.csv\nparameter_set_id: p1\nib_environment: paper\nshares: [1]\nlog_level: INFO\n",
        encoding="utf-8",
    )

    configured = resolve_live_launch_config(args(path))
    assert configured.threshold_update_rate == 0.0
    assert configured.threshold_mode is ThresholdMode.AUTO


def test_live_config_uses_numeric_threshold_as_auto_initial_value_when_rate_is_supplied(tmp_path: Path) -> None:
    path = tmp_path / "live.yaml"
    path.write_text(
        "symbol: AAPL\ndirection: BUY\nthreshold: 100\nthreshold_update_rate: 0\nparameter_set_path: params.csv\nparameter_set_id: p1\nib_environment: paper\nshares: [1]\nlog_level: INFO\n",
        encoding="utf-8",
    )

    configured = resolve_live_launch_config(args(path))

    assert configured.threshold_mode is ThresholdMode.AUTO
    assert configured.threshold == 100.0
    assert live_launch_configuration(configured)["auto_threshold_enabled"] is True


@pytest.mark.parametrize("rate", ("", -1, 100.1, True, "not-a-number", ".nan"))
def test_live_config_rejects_invalid_threshold_update_rate(tmp_path: Path, rate: object) -> None:
    path = tmp_path / "live.yaml"
    path.write_text(
        f"symbol: AAPL\ndirection: BUY\nthreshold: null\nthreshold_update_rate: {rate!r}\nparameter_set_path: params.csv\nparameter_set_id: p1\nib_environment: paper\nshares: [1]\nlog_level: INFO\n",
        encoding="utf-8",
    )

    with pytest.raises(InputValidationError, match="threshold_update_rate"):
        resolve_live_launch_config(args(path))


@pytest.mark.parametrize("shares", ("[]", "[0]", "[1.5]", "[true]", "1"))
def test_live_config_rejects_invalid_shares(tmp_path: Path, shares: str) -> None:
    path = tmp_path / "live.yaml"
    path.write_text(
        "symbol: AAPL\ndirection: BUY\nthreshold: 100\nparameter_set_path: params.csv\nparameter_set_id: p1\nib_environment: paper\n"
        f"shares: {shares}\nlog_level: INFO\n", encoding="utf-8",
    )
    with pytest.raises(InputValidationError, match="shares"):
        resolve_live_launch_config(args(path))


def test_cli_shares_accept_commas_and_spaces(tmp_path: Path) -> None:
    path = tmp_path / "live.yaml"
    path.write_text("symbol: AAPL\ndirection: BUY\nthreshold: 100\nparameter_set_path: params.csv\nparameter_set_id: p1\nib_environment: paper\nshares: [9]\nlog_level: INFO\n", encoding="utf-8")
    assert resolve_live_launch_config(args(path, shares=["1, 2", "3"])).shares == (1, 2, 3)
