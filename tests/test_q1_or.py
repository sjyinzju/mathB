from pathlib import Path

from src.solver import (
    Q1MasterConfig,
    audit_master_symmetry,
    build_frozen_incumbent_start,
    canonical_allocation_pattern,
    collect_elite_route_pool,
    export_q1_solution,
    load_q1_solution,
    load_problem_data,
    route_identity,
    solve_restricted_lp,
    solve_route_pool_master,
)
from src.solver.models import PassengerAssignment, RoutePlan, RouteStop
from src.validation import validate_solution


ROOT = Path(__file__).resolve().parents[1]


def test_route_identity_preserves_order_and_refuel_semantics():
    left = RoutePlan(
        "A01",
        "T1",
        (
            RouteStop("A01"),
            RouteStop("F001", is_service=True),
            RouteStop("F002", refuel=True),
            RouteStop("A01"),
        ),
        service_facilities=("F001",),
    )
    reordered = RoutePlan(
        "A01",
        "T1",
        (
            RouteStop("A01"),
            RouteStop("F002", refuel=True),
            RouteStop("F001", is_service=True),
            RouteStop("A01"),
        ),
        service_facilities=("F001",),
    )
    no_refuel = RoutePlan(
        "A01",
        "T1",
        (
            RouteStop("A01"),
            RouteStop("F001", is_service=True),
            RouteStop("F002"),
            RouteStop("A01"),
        ),
        service_facilities=("F001",),
    )
    assert route_identity(left) != route_identity(reordered)
    assert route_identity(left) != route_identity(no_refuel)


def test_control_route_pool_master_reproduces_14770(tmp_path):
    data = load_problem_data()
    pool = collect_elite_route_pool(
        data,
        ROOT / "outputs" / "q1",
        maximum_objective=14770,
        exact_objective=14770,
    )
    config = Q1MasterConfig(
        primary_time_limit_seconds=10.0,
        secondary_time_limit_seconds=2.0,
    )
    lp = solve_restricted_lp(data, pool, config)
    result = solve_route_pool_master(data, pool, config)
    assert lp.objective == 14770.0
    assert result.primary_objective == 14770
    assert result.primary_proven_optimal
    assert result.solution.metrics.total_flights == 89
    assert result.solution.metrics.served_passengers == 1600

    routes_path = tmp_path / "q1-routes.csv"
    assignments_path = tmp_path / "q1-assignments.csv"
    export_q1_solution(result.solution, routes_path, assignments_path)
    validation = validate_solution(
        "q1", routes_path, assignments_path, data_dir=ROOT / "data" / "raw"
    )
    assert validation.valid
    assert validation.metrics is not None
    assert validation.metrics.total_aircraft_time_minutes == 14770


def test_canonical_pattern_removes_passenger_permutation_symmetry():
    left = (
        PassengerAssignment("P002", "A01", "F001", 0, 1),
        PassengerAssignment("P001", "A01", "F001", 0, 1),
        PassengerAssignment("P003", "LAND", "F001", 0, 1),
    )
    right = tuple(reversed(left))
    expected = (("A01", "F001", 2), ("LAND", "F001", 1))
    assert canonical_allocation_pattern(left) == expected
    assert canonical_allocation_pattern(right) == expected
    assert canonical_allocation_pattern(expected) == expected


def test_frozen_14730_maps_to_complete_integer_mip_start():
    data = load_problem_data()
    pool = collect_elite_route_pool(data, ROOT / "outputs" / "q1")
    frozen = load_q1_solution(
        ROOT / "outputs" / "q1" / "final" / "q1-routes.csv",
        ROOT / "outputs" / "q1" / "final" / "q1-assignments.csv",
        data,
    )
    arrays, start = build_frozen_incumbent_start(data, pool, frozen)
    assert not start.missing_patterns
    assert start.maximum_equality_residual == 0.0
    assert start.selected_columns == 84
    assert start.flights_objective == 89
    assert start.primary_objective == 14730
    assert start.passenger_objective == 121363
    assert start.fuel_objective_kg == 118624.4
    assert float(arrays.primary @ start.values) == 14730.0


def test_master_symmetry_audit_retains_no_exact_duplicate_columns():
    data = load_problem_data()
    pool = collect_elite_route_pool(data, ROOT / "outputs" / "q1")
    audit = audit_master_symmetry(data, pool)
    assert audit["individual_passenger_identity_variables"] == 0
    assert audit["exact_duplicate_retained_columns"] == 0
    assert audit["route_id_semantic_collisions"] == 0

