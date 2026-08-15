from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_enhanced_q3_bound_report_is_internally_consistent() -> None:
    report = json.loads(
        (ROOT / "outputs/q3/best/q3-enhanced-bounds.json").read_text(
            encoding="utf-8"
        )
    )
    incumbent = report["incumbent_aircraft_time_minutes"]
    global_bounds = report["global_bounds"]
    network = global_bounds["layered_multicommodity_flow"]
    candidate = report["candidate_pool_reference"]

    assert incumbent > 0
    assert global_bounds["passenger_work_lower_bound_minutes"] == 12389
    assert global_bounds["enhanced_global_lower_bound_minutes"] == 14125
    assert network["valid_for_original_problem"] is True
    assert network["solver_status"] == 0
    assert network["objective_minutes_integer_ceiling"] == 14125
    assert global_bounds["certified_gap_percent"] == round(
        100.0 * (incumbent - 14125) / incumbent, 6
    )
    assert candidate["valid_for_original_problem"] is False
    assert candidate["objective_minutes_integer_ceiling"] == 15198


def test_bound_summary_uses_only_global_bound_for_certificate() -> None:
    report = json.loads(
        (ROOT / "outputs/q3/best/q3-bounds.json").read_text(encoding="utf-8")
    )
    stage1 = report["stage1"]

    assert stage1["enhanced_global_lower_bound_minutes"] == 14125
    assert stage1["conservative_gap_percent"] == round(
        100.0
        * (
            stage1["incumbent_upper_bound_minutes"]
            - stage1["enhanced_global_lower_bound_minutes"]
        )
        / stage1["incumbent_upper_bound_minutes"],
        6,
    )
    assert stage1["finite_candidate_pool_lp_reference_minutes"] == 15198
    assert stage1["candidate_pool_reference_is_global_bound"] is False
