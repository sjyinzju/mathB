from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ..io_utils import read_csv
from .data import ProblemData
from .evaluator import evaluate_route
from .models import (
    PassengerAssignment,
    RoutePlan,
    RouteStop,
    Solution,
    aggregate_evaluations,
)


def load_q1_solution(
    routes_path: Path | str,
    assignments_path: Path | str,
    data: ProblemData,
    *,
    method: str = "q1_imported_solution",
) -> Solution:
    person_od = {
        person_id: (pool.origin_id, pool.destination_id)
        for pool in data.q1_pools.values()
        for person_id in pool.person_ids
    }
    route_rows: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    assignment_rows: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(Path(routes_path)):
        route_rows[(row["aircraft_type"], int(row["flight_no"]))].append(row)
    for row in read_csv(Path(assignments_path)):
        assignment_rows[(row["aircraft_type"], int(row["flight_no"]))].append(row)

    routes: list[RoutePlan] = []
    evaluations = []
    for key, rows in sorted(route_rows.items()):
        rows.sort(key=lambda row: int(row["stop_order"]))
        stops = tuple(
            RouteStop(row["facility_id"], refuel=bool(int(row["refuel"])))
            for row in rows
        )
        assignments = []
        for row in assignment_rows.get(key, []):
            person_id = row["person_id"]
            if person_id not in person_od:
                raise ValueError(f"Unknown Q1 person in imported solution: {person_id}")
            origin, destination = person_od[person_id]
            assignments.append(
                PassengerAssignment(
                    person_id=person_id,
                    origin_id=origin,
                    destination_id=destination,
                    pickup_stop_order=int(row["pickup_stop_order"]),
                    delivery_stop_order=int(row["delivery_stop_order"]),
                )
            )
        assignments.sort(key=lambda item: item.person_id)
        locations = tuple(stop.facility_id for stop in stops)
        service_order = tuple(
            dict.fromkeys(
                locations[item.delivery_stop_order]
                for item in sorted(assignments, key=lambda value: value.delivery_stop_order)
            )
        )
        route = RoutePlan(
            base_airport=stops[0].facility_id,
            aircraft_type=key[0],
            stops=stops,
            assignments=tuple(assignments),
            service_facilities=service_order,
        )
        evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
        if not evaluation.feasible:
            raise ValueError(f"Imported route {key} is infeasible: {evaluation.issues}")
        routes.append(route)
        evaluations.append(evaluation)

    assigned = [item.person_id for route in routes for item in route.assignments]
    if len(assigned) != len(set(assigned)) or set(assigned) != set(person_od):
        raise ValueError("Imported Q1 solution does not cover every person exactly once")
    return Solution(
        routes=tuple(routes),
        metrics=aggregate_evaluations(evaluations, served=len(assigned)),
        method=method,
    )


def load_q2_solution(
    routes_path: Path | str,
    assignments_path: Path | str,
    data: ProblemData,
    *,
    method: str = "q2_imported_solution",
) -> Solution:
    person_od = {
        person_id: (pool.origin_id, pool.destination_id)
        for pool in data.q2_pools.values()
        for person_id in pool.person_ids
    }
    route_rows: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    assignment_rows: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(Path(routes_path)):
        route_rows[(row["aircraft_type"], int(row["flight_no"]))].append(row)
    for row in read_csv(Path(assignments_path)):
        assignment_rows[(row["aircraft_type"], int(row["flight_no"]))].append(row)

    routes: list[RoutePlan] = []
    evaluations = []
    for key, rows in sorted(route_rows.items()):
        rows.sort(key=lambda row: int(row["stop_order"]))
        stops = tuple(
            RouteStop(row["facility_id"], refuel=bool(int(row["refuel"])))
            for row in rows
        )
        assignments = []
        service_positions: set[int] = set()
        for row in assignment_rows.get(key, []):
            person_id = row["person_id"]
            if person_id not in person_od:
                raise ValueError(f"Unknown Q2 person in imported solution: {person_id}")
            origin, destination = person_od[person_id]
            pickup = int(row["pickup_stop_order"])
            delivery = int(row["delivery_stop_order"])
            if 0 < pickup < len(stops) - 1:
                service_positions.add(pickup)
            if 0 < delivery < len(stops) - 1:
                service_positions.add(delivery)
            assignments.append(
                PassengerAssignment(
                    person_id=person_id,
                    origin_id=origin,
                    destination_id=destination,
                    pickup_stop_order=pickup,
                    delivery_stop_order=delivery,
                )
            )
        service_order = tuple(stops[index].facility_id for index in sorted(service_positions))
        route = RoutePlan(
            base_airport=stops[0].facility_id,
            aircraft_type=key[0],
            stops=stops,
            assignments=tuple(sorted(assignments, key=lambda item: item.person_id)),
            service_facilities=tuple(dict.fromkeys(service_order)),
        )
        evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
        if not evaluation.feasible:
            raise ValueError(f"Imported Q2 route {key} is infeasible: {evaluation.issues}")
        routes.append(route)
        evaluations.append(evaluation)

    assigned = [item.person_id for route in routes for item in route.assignments]
    if len(assigned) != len(set(assigned)) or set(assigned) != set(person_od):
        raise ValueError("Imported Q2 solution does not cover every person exactly once")
    return Solution(
        routes=tuple(routes),
        metrics=aggregate_evaluations(evaluations, served=len(assigned)),
        method=method,
    )
