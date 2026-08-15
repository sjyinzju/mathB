from __future__ import annotations

from src.solver.data import load_problem_data
from src.solver.evaluator import evaluate_route
from src.solver.models import PassengerAssignment, RoutePlan
from src.solver.q2 import assignment_interval, build_q2_variant


def test_q2_data_counts() -> None:
    data = load_problem_data()
    assert data.q2_passenger_count == 4000
    assert len(data.q2_pools) == 264


def test_q2_pair_route_reuses_capacity() -> None:
    data = load_problem_data()
    variant = build_q2_variant(data, "A01", "T3", ("F021", "F022"))
    assert variant is not None
    intervals = [
        assignment_interval(variant, "LAND", "F021", data.config.airports),
        assignment_interval(variant, "F021", "F022", data.config.airports),
        assignment_interval(variant, "F022", "LAND", data.config.airports),
    ]
    assert all(interval is not None for interval in intervals)
    assignments = tuple(
        PassengerAssignment(
            person_id=f"TEST{index}",
            origin_id=origin,
            destination_id=destination,
            pickup_stop_order=interval[0],
            delivery_stop_order=interval[1],
        )
        for index, (origin, destination, interval) in enumerate(
            zip(
                ("LAND", "F021", "F022"),
                ("F021", "F022", "LAND"),
                intervals,
            )
        )
        if interval is not None
    )
    route = RoutePlan(
        base_airport=variant.base_airport,
        aircraft_type=variant.aircraft_type,
        stops=variant.route.stops,
        assignments=assignments,
        service_facilities=variant.service_order,
    )
    evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
    assert evaluation.feasible
    assert len(assignments) == 3
