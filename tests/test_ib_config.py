from __future__ import annotations

import pytest

from single_day_test.domain.errors import InputValidationError
from single_day_test.ib.config import IbConfig


def test_loads_selected_yaml_profile(tmp_path) -> None:
    path = tmp_path / "ib.yaml"
    path.write_text("paper:\n  host: 127.0.0.1\n  port: 7497\n  client_id: 71\n  connect_timeout: 10\nlive:\n  host: 127.0.0.1\n  port: 7496\n  client_id: 72\n  connect_timeout: 15\n", encoding="utf-8")
    assert IbConfig.from_yaml(path, "live") == IbConfig("127.0.0.1", 7496, 72, 15.0)


def test_rejects_missing_yaml_profile(tmp_path) -> None:
    path = tmp_path / "ib.yaml"; path.write_text("paper: {}\n", encoding="utf-8")
    with pytest.raises(InputValidationError, match="Invalid IB YAML profile"):
        IbConfig.from_yaml(path, "live")
