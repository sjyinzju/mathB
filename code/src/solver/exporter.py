from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..io_utils import read_csv, write_csv
from ..validation.solution_validator import Q12_ASSIGNMENT_SCHEMA, Q12_ROUTE_SCHEMA
from .data import ProblemData
from .evaluator import evaluate_route
from .models import (
    PassengerAssignment,
    RoutePlan,
    RouteStop,
    Solution,
    aggregate_evaluations,
)


@dataclass(frozen=True)
class ExportResult:
    routes_path: Path
    assignments_path: Path
    route_rows: int
    assignment_rows: int


def load_q1_solution(
    routes_path: Path | str,
    assignments_path: Path | str,
    data: ProblemData,
    *,
    method: str = "q1_loaded_solution",
) -> Solution:
    """Restore the internal route model from official Q1 CSV files."""
    route_rows = read_csv(routes_path)
    assignment_rows = read_csv(assignments_path)
    demand_lookup = {
        person_id: (pool.origin_id, pool.destination_id)
        for pool in data.q1_pools.values()
        for person_id in pool.person_ids
    }
    routes_by_flight: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    assignments_by_flight: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in route_rows:
        routes_by_flight[(row["aircraft_type"], int(row["flight_no"]))].append(row)
    for row in assignment_rows:
        assignments_by_flight[(row["aircraft_type"], int(row["flight_no"]))].append(row)

    routes: list[RoutePlan] = []
    evaluations = []
    # Preserve first appearance order so loading and re-exporting an official
    # solution is byte-stable even when different aircraft types are interleaved.
    for key in routes_by_flight:
        ordered_rows = sorted(routes_by_flight[key], key=lambda row: int(row["stop_order"]))
        delivery_orders = {
            int(row["delivery_stop_order"])
            for row in assignments_by_flight.get(key, [])
        }
        stops = tuple(
            RouteStop(
                row["facility_id"],
                refuel=bool(int(row["refuel"])),
                is_service=index in delivery_orders,
            )
            for index, row in enumerate(ordered_rows)
        )
        assignments: list[PassengerAssignment] = []
        for row in sorted(assignments_by_flight.get(key, []), key=lambda item: item["person_id"]):
            person_id = row["person_id"]
            if person_id not in demand_lookup:
                raise ValueError(f"PERSON_MAPPING: unknown person {person_id}")
            origin, destination = demand_lookup[person_id]
            assignments.append(
                PassengerAssignment(
                    person_id,
                    origin,
                    destination,
                    int(row["pickup_stop_order"]),
                    int(row["delivery_stop_order"]),
                )
            )
        service_facilities = tuple(
            stops[index].facility_id for index in sorted(delivery_orders)
        )
        route = RoutePlan(
            base_airport=stops[0].facility_id,
            aircraft_type=key[0],
            stops=stops,
            assignments=tuple(assignments),
            service_facilities=service_facilities,
        )
        evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
        if not evaluation.feasible:
            raise ValueError(f"Loaded route {key} is infeasible: {evaluation.issues}")
        routes.append(route)
        evaluations.append(evaluation)
    assigned_people = [item.person_id for route in routes for item in route.assignments]
    if len(assigned_people) != len(set(assigned_people)):
        raise ValueError("PERSON_MAPPING: duplicate people in loaded solution")
    return Solution(
        routes=tuple(routes),
        metrics=aggregate_evaluations(evaluations, served=len(assigned_people)),
        method=method,
        diagnostics={"loaded_from": str(Path(routes_path).parent)},
    )


def load_q2_solution(
    routes_path: Path | str,
    assignments_path: Path | str,
    data: ProblemData,
    *,
    method: str = "q2_loaded_solution",
) -> Solution:
    """Restore and independently re-evaluate an official Q2 solution."""
    route_rows = read_csv(routes_path)
    assignment_rows = read_csv(assignments_path)
    demand_lookup = {
        person_id: (pool.origin_id, pool.destination_id)
        for pool in data.q2_pools.values()
        for person_id in pool.person_ids
    }
    routes_by_flight: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    assignments_by_flight: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in route_rows:
        routes_by_flight[(row["aircraft_type"], int(row["flight_no"]))].append(row)
    for row in assignment_rows:
        assignments_by_flight[(row["aircraft_type"], int(row["flight_no"]))].append(row)

    routes: list[RoutePlan] = []
    evaluations = []
    for key in routes_by_flight:
        ordered_rows = sorted(routes_by_flight[key], key=lambda row: int(row["stop_order"]))
        service_positions: set[int] = set()
        assignments: list[PassengerAssignment] = []
        for row in sorted(assignments_by_flight.get(key, []), key=lambda item: item["person_id"]):
            person_id = row["person_id"]
            if person_id not in demand_lookup:
                raise ValueError(f"PERSON_MAPPING: unknown Q2 person {person_id}")
            pickup = int(row["pickup_stop_order"])
            delivery = int(row["delivery_stop_order"])
            if 0 < pickup < len(ordered_rows) - 1:
                service_positions.add(pickup)
            if 0 < delivery < len(ordered_rows) - 1:
                service_positions.add(delivery)
            origin, destination = demand_lookup[person_id]
            assignments.append(
                PassengerAssignment(person_id, origin, destination, pickup, delivery)
            )
        stops = tuple(
            RouteStop(
                row["facility_id"],
                refuel=bool(int(row["refuel"])),
                is_service=index in service_positions,
            )
            for index, row in enumerate(ordered_rows)
        )
        route = RoutePlan(
            base_airport=stops[0].facility_id,
            aircraft_type=key[0],
            stops=stops,
            assignments=tuple(assignments),
            service_facilities=tuple(stops[index].facility_id for index in sorted(service_positions)),
        )
        evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
        if not evaluation.feasible:
            raise ValueError(f"Loaded Q2 route {key} is infeasible: {evaluation.issues}")
        routes.append(route)
        evaluations.append(evaluation)

    assigned_people = [item.person_id for route in routes for item in route.assignments]
    expected_people = set(demand_lookup)
    if len(assigned_people) != len(set(assigned_people)):
        raise ValueError("PERSON_MAPPING: duplicate people in loaded Q2 solution")
    if set(assigned_people) != expected_people:
        missing = len(expected_people - set(assigned_people))
        extra = len(set(assigned_people) - expected_people)
        raise ValueError(f"PERSON_MAPPING: Q2 coverage mismatch missing={missing}, extra={extra}")
    return Solution(
        routes=tuple(routes),
        metrics=aggregate_evaluations(evaluations, served=len(assigned_people)),
        method=method,
        diagnostics={"loaded_from": str(Path(routes_path).parent)},
    )


def export_q1_solution(
    solution: Solution,
    routes_path: Path | str,
    assignments_path: Path | str,
) -> ExportResult:
    routes_path = Path(routes_path)
    assignments_path = Path(assignments_path)
    counters: dict[str, int] = defaultdict(int)
    route_rows: list[dict[str, object]] = []
    assignment_rows: list[dict[str, object]] = []
    seen_people: set[str] = set()

    for route in solution.routes:
        counters[route.aircraft_type] += 1
        flight_no = counters[route.aircraft_type]
        for stop_order, stop in enumerate(route.stops):
            route_rows.append(
                {
                    "aircraft_type": route.aircraft_type,
                    "flight_no": flight_no,
                    "stop_order": stop_order,
                    "facility_id": stop.facility_id,
                    "refuel": int(stop.refuel),
                }
            )
        for assignment in route.assignments:
            if assignment.person_id in seen_people:
                raise ValueError(f"PERSON_MAPPING: duplicate assignment for {assignment.person_id}")
            seen_people.add(assignment.person_id)
            assignment_rows.append(
                {
                    "person_id": assignment.person_id,
                    "aircraft_type": route.aircraft_type,
                    "flight_no": flight_no,
                    "pickup_stop_order": assignment.pickup_stop_order,
                    "delivery_stop_order": assignment.delivery_stop_order,
                }
            )
    assignment_rows.sort(key=lambda row: str(row["person_id"]))
    write_csv(routes_path, Q12_ROUTE_SCHEMA, route_rows)
    write_csv(assignments_path, Q12_ASSIGNMENT_SCHEMA, assignment_rows)
    return ExportResult(routes_path, assignments_path, len(route_rows), len(assignment_rows))
