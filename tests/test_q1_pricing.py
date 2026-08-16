from __future__ import annotations

import random
from collections import Counter
from itertools import product
from pathlib import Path

import pytest

from src.solver import (
    PRICING_TOL,
    ExactRouteColumn,
    choose_fractional_arc_branch,
    column_arc_counts,
    collect_elite_route_pool,
    exact_pricing,
    initial_exact_columns,
    load_problem_data,
    solve_fullspace_rmp_lp,
    solve_exact_column_rmp_lp,
)
from src.solver.data import ProblemData
from src.solver.evaluator import evaluate_route
from src.solver.models import DemandPool, RoutePlan, RouteStop
from src.solver.q1_pricing import column_reduced_cost


ROOT = Path(__file__).resolve().parents[1]


def _tiny_data() -> ProblemData:
    full = load_problem_data()
    pools = {
        ("A01", "F001"): DemandPool("A01", "F001", ("P1", "P2")),
        ("LAND", "F006"): DemandPool("LAND", "F006", ("P3", "P4", "P5")),
        ("A01", "F010"): DemandPool("A01", "F010", ("P6",)),
        ("A02", "F001"): DemandPool("A02", "F001", ("P7", "P8")),
    }
    return ProblemData(full.config, full.matrix, pools)


def _best_allocation(data, duals, base, aircraft_type, visited):
    capacity = data.config.aircraft_types[aircraft_type].seats
    units = []
    for key, pool in data.q1_pools.items():
        if key[1] not in visited or key[0] not in (base, "LAND"):
            continue
        units.extend((float(duals[key]), key) for _ in range(pool.quantity))
    units.sort(reverse=True)
    if not units:
        return None
    chosen = [unit for unit in units[:capacity] if unit[0] > 0.0]
    if not chosen:
        chosen = units[:1]
    counts = Counter(key for _, key in chosen)
    return tuple((origin, destination, count)
                 for (origin, destination), count in sorted(counts.items()))


def _brute_force_price(data, duals, base, aircraft_type, nodes, max_landings):
    best = None
    for length in range(1, max_landings + 1):
        for sequence in product(nodes, repeat=length):
            refuel_options = [
                (False, True) if node in data.config.refuel_facilities else (False,)
                for node in sequence
            ]
            for flags in product(*refuel_options):
                stops = (RouteStop(base),) + tuple(
                    RouteStop(node, refuel=flag) for node, flag in zip(sequence, flags)
                ) + (RouteStop(base),)
                route = RoutePlan(base, aircraft_type, stops)
                evaluation = evaluate_route(
                    route, matrix=data.matrix, config=data.config
                )
                if not evaluation.feasible:
                    continue
                pattern = _best_allocation(
                    data, duals, base, aircraft_type, set(sequence)
                )
                if pattern is None:
                    continue
                reduced_cost = column_reduced_cost(
                    evaluation.total_aircraft_time_minutes, pattern, duals
                )
                candidate = (reduced_cost, evaluation.total_aircraft_time_minutes, pattern)
                if best is None or candidate < best:
                    best = candidate
    assert best is not None
    return best


def test_standard_rmp_removes_fractional_column_caps():
    data = load_problem_data()
    pool = collect_elite_route_pool(data, ROOT / "outputs" / "q1")
    result = solve_fullspace_rmp_lp(data, pool)
    assert result.objective == pytest.approx(14199.521097474075)
    assert result.reduced_costs.min() >= -1.0e-7
    columns = initial_exact_columns(data, pool)
    rebuilt = solve_exact_column_rmp_lp(data, columns)
    assert rebuilt.objective == pytest.approx(result.objective)
    assert len(columns) == 1003


def test_exact_pricing_matches_tiny_exhaustive_with_refuel_and_repeats():
    data = _tiny_data()
    nodes = ("F001", "F006", "F010")
    duals = {
        ("A01", "F001"): 13.25,
        ("LAND", "F006"): 9.5,
        ("A01", "F010"): 7.75,
        ("A02", "F001"): 1000.0,
    }
    brute = _brute_force_price(data, duals, "A01", "T1", nodes, 3)
    priced = exact_pricing(
        data,
        duals,
        "A01",
        "T1",
        candidate_nodes=nodes,
        max_landings=3,
    )
    assert priced.proven_optimal
    assert priced.reduced_cost == pytest.approx(brute[0], abs=1.0e-7)
    assert priced.route_duration_minutes == brute[1]
    assert ("A02", "F001", 2) not in priced.allocation_pattern


@pytest.mark.parametrize("seed", range(5))
def test_randomized_tiny_pricing_matches_complete_enumeration(seed):
    data = _tiny_data()
    nodes = ("F001", "F006", "F010")
    rng = random.Random(seed)
    duals = {key: rng.uniform(-5.0, 25.0) for key in data.q1_pools}
    brute = _brute_force_price(data, duals, "A01", "T2", nodes, 2)
    priced = exact_pricing(
        data,
        duals,
        "A01",
        "T2",
        candidate_nodes=nodes,
        max_landings=2,
    )
    assert priced.proven_optimal
    assert priced.reduced_cost == pytest.approx(brute[0], abs=1.0e-7)


def test_pricing_tolerance_classifies_numerical_noise():
    assert -1.0e-9 >= -PRICING_TOL
    assert -1.0e-5 < -PRICING_TOL


def test_arc_branch_coefficients_count_repeated_traversals():
    column = ExactRouteColumn(
        "X",
        "A01",
        "T1",
        (
            RouteStop("A01"),
            RouteStop("F006"),
            RouteStop("F001"),
            RouteStop("F006"),
            RouteStop("A01"),
        ),
        (("A01", "F001", 1),),
        100,
        "test",
    )
    counts = column_arc_counts(column)
    assert counts[("A01", "F006")] == 1
    assert counts[("F006", "F001")] == 1
    assert counts[("F001", "F006")] == 1
    assert counts[("F006", "A01")] == 1
    branch = choose_fractional_arc_branch((column,), (0.5,))
    assert branch is not None
    assert branch[1] == 0.5
