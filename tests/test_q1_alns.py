"""Minimal deterministic tests for the Standard ALNS engine (Q1).

Covers: loader round-trip from the official best solution, a short ALNS run
(improvement capability + determinism) and the operator diagnostics fields.
"""
from __future__ import annotations

import pytest

from src.config import ROOT
from src.solver import (
    Q1ALNSConfig,
    SolverCache,
    improve_q1_alns,
    load_problem_data,
    load_q1_solution,
)
from src.solver.models import SolverConfig


@pytest.fixture(scope="module")
def data():
    return load_problem_data()


@pytest.fixture(scope="module")
def initial(data):
    return load_q1_solution(
        ROOT / "outputs" / "q1" / "best" / "q1-routes.csv",
        ROOT / "outputs" / "q1" / "best" / "q1-assignments.csv",
        data,
        method="q1_alns_test_initial",
    )


def _short_run(data, initial, seed: int = 0):
    """Two one-iteration stages, mirroring the multi-seed runner layout."""
    cache = SolverCache(data)
    solution = initial
    results = []
    for offset in (0, 1):
        config = Q1ALNSConfig(iterations=1, time_limit_seconds=10.0**9, seed=seed + offset)
        result = improve_q1_alns(
            solution, data, SolverConfig(seed=seed + offset), config, cache=cache
        )
        results.append(result)
        solution = result.solution
    return results


def test_initial_solution_loads_at_official_best(data, initial):
    assert initial.metrics.total_aircraft_time_minutes == pytest.approx(15371.0)
    assert initial.metrics.served_passengers == data.q1_passenger_count


def test_short_run_improves_and_is_deterministic(data, initial):
    first = _short_run(data, initial)
    second = _short_run(data, initial)
    first_time = first[-1].solution.metrics.total_aircraft_time_minutes
    assert first_time < initial.metrics.total_aircraft_time_minutes
    assert first_time == second[-1].solution.metrics.total_aircraft_time_minutes
    assert [row["operator"] for row in first[0].convergence] == [
        row["operator"] for row in second[0].convergence
    ]


def test_operator_diagnostics_fields_present(data, initial):
    result = _short_run(data, initial)[-1]
    required = {
        "operator",
        "weight",
        "calls",
        "accepted",
        "improved",
        "new_global_best",
        "feasible_repairs",
        "failed_repairs",
        "total_gain_minutes",
        "mean_gain_when_improving",
        "runtime_seconds",
        "mean_destroyed_routes",
    }
    assert result.operator_stats, "operator stats must not be empty"
    for row in result.operator_stats:
        assert required <= set(row)
    assert len(result.convergence) == 1
    convergence_keys = set(result.convergence[0])
    for key in (
        "destroyed_passengers",
        "removed_aircraft_time_minutes",
        "repair_variants",
        "repaired_routes",
    ):
        assert key in convergence_keys
    # one iteration never crosses a segment boundary, so the history is empty
    assert result.weight_history == ()
