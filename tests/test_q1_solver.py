from __future__ import annotations

from pathlib import Path

from src.config import ROOT
from src.data_pipeline import load_distance_matrix
from src.io_utils import write_csv
from src.solver.evaluator import evaluate_route
from src.solver.exporter import export_q1_solution
from src.solver.improve import improve_q1_savings
from src.solver.data import load_problem_data
from src.solver.models import (
    PassengerAssignment,
    RoutePlan,
    RouteStop,
    Solution,
    SolutionMetrics,
    SolverConfig,
    aggregate_evaluations,
)
from src.solver.technical_stops import augment_service_sequence
from src.validation import validate_solution


def test_technical_stop_search_returns_concrete_refuel_witness(config, raw_dir):
    _, matrix = load_distance_matrix(raw_dir / "distances.csv")
    result = augment_service_sequence(
        "A01",
        "T1",
        ("F006",),
        matrix=matrix,
        config=config,
    )
    assert result.feasible, result.reason
    assert [stop.facility_id for stop in result.stops] == ["A01", "F006", "A01"]
    assert result.stops[1].refuel
    assert result.stops[1].is_service


def test_secondary_objective_order_is_configurable_but_primary_stays_first():
    metrics = SolutionMetrics(100, 300, 4, 500.0, 0.75, 20)
    policy = SolverConfig(
        secondary_order=(
            "seat_utilization",
            "total_fuel_consumption_kg",
            "total_flights",
            "total_passenger_travel_time_minutes",
        )
    )
    assert metrics.comparison_key(policy.secondary_order) == (100.0, -0.75, 500.0, 4.0, 300.0)


def test_internal_evaluator_reports_capacity_and_first_destination(config, raw_dir):
    _, matrix = load_distance_matrix(raw_dir / "distances.csv")
    assignments = tuple(
        PassengerAssignment(
            person_id=f"X{index:02d}",
            origin_id="LAND",
            destination_id="F006",
            pickup_stop_order=0,
            delivery_stop_order=3,
        )
        for index in range(13)
    )
    route = RoutePlan(
        base_airport="A01",
        aircraft_type="T1",
        stops=(
            RouteStop("A01"),
            RouteStop("F006", refuel=True),
            RouteStop("F020"),
            RouteStop("F006", refuel=True),
            RouteStop("A01"),
        ),
        assignments=assignments,
    )
    result = evaluate_route(route, matrix=matrix, config=config)
    assert not result.feasible
    assert any(issue.startswith("CAPACITY_VIOLATION") for issue in result.issues)
    assert any(issue.startswith("NOT_FIRST_DESTINATION_STOP") for issue in result.issues)


def test_exported_q1_solution_round_trips_through_validator(tmp_path, small_data_dir, config):
    write_csv(
        small_data_dir / "peopleQ1.csv",
        ["person_id", "origin_id", "destination_id"],
        [{"person_id": "P0001", "origin_id": "LAND", "destination_id": "F014"}],
    )
    route = RoutePlan(
        base_airport="A01",
        aircraft_type="T3",
        stops=(RouteStop("A01"), RouteStop("F014", is_service=True), RouteStop("A01")),
        assignments=(PassengerAssignment("P0001", "LAND", "F014", 0, 1),),
        service_facilities=("F014",),
    )
    solution = Solution(
        routes=(route,),
        metrics=SolutionMetrics(124, 52, 1, 951.2, 1 / 19, 1),
    )
    routes_path = tmp_path / "q1-routes.csv"
    assignments_path = tmp_path / "q1-assignments.csv"
    export_q1_solution(solution, routes_path, assignments_path)
    result = validate_solution(
        "q1", routes_path, assignments_path, data_dir=small_data_dir, config=config
    )
    assert result.valid, [str(issue) for issue in result.issues]
    assert result.metrics is not None
    assert result.metrics.served_passengers == 1


def test_generalized_savings_uses_exact_route_evaluation(config, raw_dir):
    data = load_problem_data(config=config)
    routes = []
    evaluations = []
    for person_id, destination in (("X01", "F014"), ("X02", "F020")):
        augmented = augment_service_sequence(
            "A01", "T3", (destination,), matrix=data.matrix, config=config
        )
        delivery = [stop.facility_id for stop in augmented.stops].index(destination)
        route = RoutePlan(
            base_airport="A01",
            aircraft_type="T3",
            stops=augmented.stops,
            assignments=(PassengerAssignment(person_id, "LAND", destination, 0, delivery),),
            service_facilities=(destination,),
        )
        routes.append(route)
        evaluations.append(evaluate_route(route, matrix=data.matrix, config=config))
    baseline = Solution(tuple(routes), aggregate_evaluations(evaluations, served=2))
    improved = improve_q1_savings(baseline, data, max_neighbors=2)
    assert len(improved.routes) == 1
    assert improved.metrics.total_aircraft_time_minutes < baseline.metrics.total_aircraft_time_minutes


def test_checked_in_q1_baseline_serves_all_people_and_validates(config, raw_dir):
    best = ROOT / "outputs" / "q1" / "best"
    result = validate_solution(
        "q1",
        best / "q1-routes.csv",
        best / "q1-assignments.csv",
        data_dir=raw_dir,
        config=config,
    )
    assert result.valid, [str(issue) for issue in result.issues]
    assert result.metrics is not None
    assert result.metrics.served_passengers == 1600
    assert result.metrics.total_flights <= 110
    assert result.metrics.total_aircraft_time_minutes <= 17222
