from pathlib import Path

from src.solver import (
    Q1MasterConfig,
    collect_elite_route_pool,
    export_q1_solution,
    load_problem_data,
    route_identity,
    solve_restricted_lp,
    solve_route_pool_master,
)
from src.solver.models import RoutePlan, RouteStop
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

