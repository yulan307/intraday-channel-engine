from __future__ import annotations

import pytest

from single_day_test.domain.errors import InputValidationError
from single_day_test.domain.parameters import load_parameter_set, load_parameter_sets


def test_load_parameter_set_by_id(tmp_path) -> None:
    path = tmp_path / "parameter_set.csv"
    path.write_text("parameter_set_id,trend_window,channel_window,r2_threshold,channel_high_percentile,channel_low_percentile,continuous_break_count,curr_mix_ratio,is_active\nalpha,20,10,0.5,95,95,2,0.25,0\n", encoding="utf-8")
    params = load_parameter_set(path, "alpha")
    assert params.parameter_set_id == "alpha" and params.trend_window == 20
    assert params.curr_mix_ratio == 0.25


def test_parameter_csv_requires_one_matching_id(tmp_path) -> None:
    path = tmp_path / "parameter_set.csv"; path.write_text("parameter_set_id,trend_window\nalpha,20\n", encoding="utf-8")
    with pytest.raises(InputValidationError):
        load_parameter_set(path, "alpha")


def test_active_selection_and_explicit_inactive_override(tmp_path) -> None:
    path = tmp_path / "parameter_set.csv"
    path.write_text(
        "parameter_set_id,trend_window,channel_window,r2_threshold,channel_high_percentile,channel_low_percentile,continuous_break_count,curr_mix_ratio,is_active\n"
        "active,20,10,0.5,95,95,2,0.0,1\n"
        "inactive,20,10,0.5,95,95,2,1.0,0\n",
        encoding="utf-8",
    )
    assert [item.parameter_set_id for item in load_parameter_sets(path)] == ["active"]
    assert [item.parameter_set_id for item in load_parameter_sets(path, "inactive")] == ["inactive"]


@pytest.mark.parametrize("ratio", ("-0.1", "1.1"))
def test_parameter_csv_rejects_curr_mix_ratio_outside_zero_to_one(tmp_path, ratio: str) -> None:
    path = tmp_path / "parameter_set.csv"
    path.write_text(
        "parameter_set_id,trend_window,channel_window,r2_threshold,channel_high_percentile,channel_low_percentile,continuous_break_count,curr_mix_ratio,is_active\n"
        f"alpha,20,10,0.5,95,95,2,{ratio},1\n",
        encoding="utf-8",
    )

    with pytest.raises(InputValidationError, match="curr_mix_ratio"):
        load_parameter_set(path, "alpha")
