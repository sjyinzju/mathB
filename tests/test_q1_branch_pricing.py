from __future__ import annotations

import random
from collections import Counter
from itertools import product

import pytest

from src.solver import (
    ArcBranchRow,
    ExactRouteColumn,
    branch_column_reduced_cost,
    exact_pricing,
    load_problem_data,
    solve_branch_rmp_lp,
)
from src.solver.data import ProblemData
from src.solver.evaluator import evaluate_route
from src.solver.models import DemandPool, RoutePlan, RouteStop


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
    return tuple(
        (origin, destination, count)
        for (origin, destination), count in sorted(counts.items())
    )


def _brute_force_branch_price(
    data,
    duals,
    base,
    aircraft_type,
    nodes,
    max_landings,
    branch_duals,
    route_cost_multiplier=1.0,
):
    best = None
    for length in range(1, max_landings + 1):
        for sequence in product(nodes, repeat=length):
            options = [
                (False, True) if node in data.config.refuel_facilities else (False,)
                for node in sequence
            ]
            for flags in product(*options):
                stops = (RouteStop(base),) + tuple(
                    RouteStop(node, refuel=flag)
                    for node, flag in zip(sequence, flags)
                ) + (RouteStop(base),)
                evaluation = evaluate_route(
                    RoutePlan(base, aircraft_type, stops),
                    matrix=data.matrix,
                    config=data.config,
                )
                if not evaluation.feasible:
                    continue
                pattern = _best_allocation(
                    data, duals, base, aircraft_type, set(sequence)
                )
                if pattern is None:
                    continue
                column = ExactRouteColumn(
                    "brute",
                    base,
                    aircraft_type,
                    stops,
                    pattern,
                    evaluation.total_aircraft_time_minutes,
                    "test",
                )
                rc = branch_column_reduced_cost(
                    column.duration_minutes,
                    pattern,
                    duals,
                    column,
                    branch_duals,
                    route_cost_multiplier=route_cost_multiplier,
                )
                candidate = (rc, column.duration_minutes, pattern)
                if best is None or candidate < best:
                    best = candidate
    assert best is not None
    return best


@pytest.mark.parametrize(
    "branch_duals",
    [
        {ArcBranchRow(("A01", "F001"), "<=", 1): -7.0},
        {ArcBranchRow(("A01", "F001"), ">=", 1): -7.0},
        {
            ArcBranchRow(("A01", "F001"), "<=", 1): -3.5,
            ArcBranchRow(("F001", "F006"), ">=", 1): -5.25,
        },
        {
            ArcBranchRow(("A01", "F001"), "<=", 1): -1.0,
            ArcBranchRow(("F001", "F006"), ">=", 1): -2.0,
            ArcBranchRow(("F006", "A01"), "<=", 1): -4.0,
        },
    ],
)
def test_branch_pricing_matches_complete_finite_universe(branch_duals):
    data = _tiny_data()
    nodes = ("F001", "F006", "F010")
    duals = {
        ("A01", "F001"): 13.25,
        ("LAND", "F006"): 9.5,
        ("A01", "F010"): 7.75,
        ("A02", "F001"): 1000.0,
    }
    brute = _brute_force_branch_price(
        data, duals, "A01", "T1", nodes, 3, branch_duals
    )
    priced = exact_pricing(
        data,
        duals,
        "A01",
        "T1",
        candidate_nodes=nodes,
        max_landings=3,
        branch_duals=branch_duals,
    )
    assert priced.proven_optimal
    assert priced.reduced_cost == pytest.approx(brute[0], abs=1.0e-7)


@pytest.mark.parametrize("seed", range(5))
def test_randomized_branch_pricing_matches_brute_force(seed):
    data = _tiny_data()
    nodes = ("F001", "F006", "F010")
    rng = random.Random(seed)
    duals = {key: rng.uniform(-5.0, 25.0) for key in data.q1_pools}
    rows = (
        ArcBranchRow(("A01", "F001"), rng.choice(("<=", ">=")), 1),
        ArcBranchRow(("F001", "F006"), rng.choice(("<=", ">=")), 1),
    )
    branch_duals = {row: -rng.uniform(0.1, 12.0) for row in rows}
    brute = _brute_force_branch_price(
        data, duals, "A01", "T2", nodes, 2, branch_duals
    )
    priced = exact_pricing(
        data,
        duals,
        "A01",
        "T2",
        candidate_nodes=nodes,
        max_landings=2,
        branch_duals=branch_duals,
    )
    assert priced.proven_optimal
    assert priced.reduced_cost == pytest.approx(brute[0], abs=1.0e-7)


def test_repeated_traversal_uses_count_not_presence():
    data = _tiny_data()
    duals = {key: 1.0 for key in data.q1_pools}
    row = ArcBranchRow(("F001", "F006"), ">=", 2)
    priced = exact_pricing(
        data,
        duals,
        "A01",
        "T2",
        candidate_nodes=("F001", "F006"),
        max_landings=4,
        branch_duals={row: -200.0},
    )
    assert priced.proven_optimal
    column = ExactRouteColumn(
        "priced",
        priced.base_airport,
        priced.aircraft_type,
        priced.stops,
        priced.allocation_pattern,
        int(priced.route_duration_minutes),
        "test",
    )
    assert row.coefficient(column) == 2
    assert priced.branch_coefficients == (2,)


def test_adjacent_repeated_visit_self_arc_is_branchable():
    row = ArcBranchRow(("F001", "F001"), "<=", 1)
    column = ExactRouteColumn(
        "self-repeat",
        "A01",
        "T1",
        (
            RouteStop("A01"), RouteStop("F001"), RouteStop("F001"),
            RouteStop("F001"), RouteStop("A01"),
        ),
        (("A01", "F001", 1),),
        100,
        "test",
    )
    assert row.coefficient(column) == 2


def test_node_rmp_canonical_dual_sign_for_left_and_right_rows():
    full = load_problem_data()
    data = ProblemData(
        full.config,
        full.matrix,
        {("A01", "F001"): DemandPool("A01", "F001", ("P1", "P2"))},
    )
    direct = ExactRouteColumn(
        "direct", "A01", "T1",
        (RouteStop("A01"), RouteStop("F001"), RouteStop("A01")),
        (("A01", "F001", 1),), 10, "test",
    )
    indirect = ExactRouteColumn(
        "indirect", "A01", "T1",
        (
            RouteStop("A01"), RouteStop("F006"),
            RouteStop("F001"), RouteStop("A01"),
        ),
        (("A01", "F001", 2),), 25, "test",
    )
    left = ArcBranchRow(("A01", "F001"), "<=", 1)
    left_result = solve_branch_rmp_lp(data, (direct, indirect), (left,))
    assert left_result.proven_optimal
    assert left_result.objective == pytest.approx(22.5)
    assert left_result.branch_duals[left] == pytest.approx(-2.5)
    assert left_result.reduced_costs.min() >= -1.0e-8

    cheap_indirect = ExactRouteColumn(
        "cheap-indirect", "A01", "T1", indirect.stops,
        indirect.allocation_pattern, 18, "test",
    )
    right = ArcBranchRow(("A01", "F001"), ">=", 1)
    right_result = solve_branch_rmp_lp(data, (direct, cheap_indirect), (right,))
    assert right_result.proven_optimal
    assert right_result.objective == pytest.approx(19.0)
    assert right_result.branch_duals[right] < 0.0
    assert right_result.reduced_costs.min() >= -1.0e-8


def test_phase_one_pricing_objective_matches_complete_enumeration():
    data = _tiny_data()
    nodes = ("F001", "F006", "F010")
    duals = {key: value for key, value in zip(data.q1_pools, (0.4, -0.2, 0.8, 9.0))}
    row = ArcBranchRow(("A01", "F001"), ">=", 2)
    branch_duals = {row: -0.7}
    brute = _brute_force_branch_price(
        data,
        duals,
        "A01",
        "T2",
        nodes,
        2,
        branch_duals,
        route_cost_multiplier=0.0,
    )
    priced = exact_pricing(
        data,
        duals,
        "A01",
        "T2",
        candidate_nodes=nodes,
        max_landings=2,
        branch_duals=branch_duals,
        route_cost_multiplier=0.0,
    )
    assert priced.proven_optimal
    assert priced.reduced_cost == pytest.approx(brute[0], abs=1.0e-7)
