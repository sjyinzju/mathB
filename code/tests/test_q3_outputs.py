from __future__ import annotations

import json
from pathlib import Path

from src.solver import load_problem_data
from src.validation import validate_solution


ROOT = Path(__file__).resolve().parents[1]


def test_promoted_q3_solution_is_valid() -> None:
    result = validate_solution(
        "q3",
        ROOT / "outputs/q3/best/q3-routes.csv",
        ROOT / "outputs/q3/best/q3-assignments.csv",
        data_dir=ROOT / "data/raw",
        config=load_problem_data().config,
    )
    assert result.valid, result.issues
    assert result.metrics is not None
    assert result.metrics.served_passengers == 3998
    assert result.metrics.unserved_optional_passengers == 2
    assert result.metrics.total_aircraft_time_minutes == 29659


def test_q3_reported_bounds_are_consistent() -> None:
    bounds = json.loads(
        (ROOT / "outputs/q3/best/q3-bounds.json").read_text(encoding="utf-8")
    )
    assert bounds["stage1"]["enhanced_global_lower_bound_minutes"] == 14125
    assert bounds["stage1"]["incumbent_upper_bound_minutes"] == 29659
    assert bounds["stage1"]["candidate_pool_reference_is_global_bound"] is False
    assert bounds["stage2"]["served_optional_incumbent"] == 158
    assert bounds["stage2"]["optional_upper_bound"] == 160
    assert bounds["stage2"]["proven_optimal_for_original_problem"] is False
    assert bounds["stage2"]["fixed_flight_assignment_optimal_only"] is True
