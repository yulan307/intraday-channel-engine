from __future__ import annotations

import pytest

from single_day_test.domain.errors import InputValidationError
from single_day_test.domain.parameters import load_parameter_set


def test_load_parameter_set_by_id(tmp_path) -> None:
    path = tmp_path / "parameter_set.csv"
    path.write_text("parameter_set_id,trend_window,slope_std_window,dev_window,residual_window,r2_threshold,channel_high_percentile,channel_low_percentile,continuous_break_count\nalpha,20,10,20,20,0.5,95,95,2\n", encoding="utf-8")
    params = load_parameter_set(path, "alpha")
    assert params.parameter_set_id == "alpha" and params.trend_window == 20


def test_parameter_csv_requires_one_matching_id(tmp_path) -> None:
    path = tmp_path / "parameter_set.csv"; path.write_text("parameter_set_id,trend_window\nalpha,20\n", encoding="utf-8")
    with pytest.raises(InputValidationError):
        load_parameter_set(path, "alpha")
