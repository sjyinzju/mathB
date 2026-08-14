from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..io_utils import write_csv
from ..validation.solution_validator import Q12_ASSIGNMENT_SCHEMA, Q12_ROUTE_SCHEMA
from .models import Solution


@dataclass(frozen=True)
class ExportResult:
    routes_path: Path
    assignments_path: Path
    route_rows: int
    assignment_rows: int


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
