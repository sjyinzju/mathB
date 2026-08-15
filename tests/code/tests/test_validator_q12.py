from __future__ import annotations

from pathlib import Path

from src.io_utils import read_csv, write_csv
from src.validation import validate_solution


def _write_q2_case(
    base: Path,
    data_dir: Path,
    *,
    capacity_violation: bool = False,
    six_stops: bool = False,
    fuel_violation: bool = False,
    repeat_destination: bool = False,
):
    people = [
        {"person_id": "E0001", "origin_id": "LAND", "destination_id": "F006"},
        {"person_id": "E0002", "origin_id": "F006", "destination_id": "F020"},
        {"person_id": "E0003", "origin_id": "F020", "destination_id": "LAND"},
    ]
    if capacity_violation:
        people = [
            {"person_id": f"E{index:04d}", "origin_id": "LAND", "destination_id": "F006"}
            for index in range(1, 14)
        ]
    write_csv(data_dir / "peopleQ2.csv", ["person_id", "origin_id", "destination_id"], people)
    route_nodes = ["A01", "F006", "F020", "A01"]
    if six_stops:
        route_nodes = ["A01", "F014", "F020", "F014", "F020", "F014", "F020", "A01"]
    if repeat_destination:
        route_nodes = ["A01", "F006", "F020", "F006", "A01"]
    aircraft_type = "T1" if capacity_violation or fuel_violation else "T2"
    routes = [
        {
            "aircraft_type": aircraft_type,
            "flight_no": 1,
            "stop_order": index,
            "facility_id": node,
            "refuel": int(node == "F006" and (index == 1 or repeat_destination)),
        }
        for index, node in enumerate(route_nodes)
    ]
    if capacity_violation:
        assignments = [
            {
                "person_id": row["person_id"],
                "aircraft_type": "T1",
                "flight_no": 1,
                "pickup_stop_order": 0,
                "delivery_stop_order": 1,
            }
            for row in people
        ]
    else:
        assignments = [
            {"person_id": "E0001", "aircraft_type": aircraft_type, "flight_no": 1, "pickup_stop_order": 0, "delivery_stop_order": 3 if repeat_destination else 1},
            {"person_id": "E0002", "aircraft_type": aircraft_type, "flight_no": 1, "pickup_stop_order": 1, "delivery_stop_order": 2},
            {"person_id": "E0003", "aircraft_type": aircraft_type, "flight_no": 1, "pickup_stop_order": 2, "delivery_stop_order": len(route_nodes) - 1},
        ]
    routes_path = base / "routes.csv"
    assignments_path = base / "assignments.csv"
    write_csv(routes_path, ["aircraft_type", "flight_no", "stop_order", "facility_id", "refuel"], routes)
    write_csv(
        assignments_path,
        ["person_id", "aircraft_type", "flight_no", "pickup_stop_order", "delivery_stop_order"],
        assignments,
    )
    return routes_path, assignments_path


def test_valid_q2_route_and_metrics(tmp_path, small_data_dir):
    routes, assignments = _write_q2_case(tmp_path, small_data_dir)
    result = validate_solution("q2", routes, assignments, data_dir=small_data_dir)
    assert result.valid, [str(issue) for issue in result.issues]
    assert result.metrics is not None
    assert result.metrics.total_flights == 1
    assert result.metrics.served_passengers == 3
    assert result.metrics.total_aircraft_time_minutes == 188


def test_capacity_violation_is_reported(tmp_path, small_data_dir):
    routes, assignments = _write_q2_case(tmp_path, small_data_dir, capacity_violation=True)
    result = validate_solution("q2", routes, assignments, data_dir=small_data_dir)
    assert not result.valid
    assert "CAPACITY_VIOLATION" in {issue.code for issue in result.issues}


def test_max_five_sea_landings(tmp_path, small_data_dir):
    routes, assignments = _write_q2_case(tmp_path, small_data_dir, six_stops=True)
    result = validate_solution("q2", routes, assignments, data_dir=small_data_dir)
    assert not result.valid
    assert "MAX_SEA_LANDINGS_EXCEEDED" in {issue.code for issue in result.issues}


def test_pickup_before_delivery_and_home_match(tmp_path, small_data_dir):
    routes, assignments = _write_q2_case(tmp_path, small_data_dir)
    rows = [
        {"person_id": "E0001", "aircraft_type": "T2", "flight_no": 1, "pickup_stop_order": 1, "delivery_stop_order": 0},
        {"person_id": "E0002", "aircraft_type": "T2", "flight_no": 1, "pickup_stop_order": 1, "delivery_stop_order": 2},
        {"person_id": "E0003", "aircraft_type": "T2", "flight_no": 1, "pickup_stop_order": 2, "delivery_stop_order": 3},
    ]
    write_csv(assignments, ["person_id", "aircraft_type", "flight_no", "pickup_stop_order", "delivery_stop_order"], rows)
    result = validate_solution("q2", routes, assignments, data_dir=small_data_dir)
    assert "PICKUP_DELIVERY_ORDER" in {issue.code for issue in result.issues}


def test_route_must_start_and_end_at_same_airport(tmp_path, small_data_dir):
    routes, assignments = _write_q2_case(tmp_path, small_data_dir)
    rows = read_csv(routes)
    rows[-1]["facility_id"] = "F014"
    write_csv(routes, list(rows[0]), rows)
    result = validate_solution("q2", routes, assignments, data_dir=small_data_dir)
    assert "ROUTE_HOME_MISMATCH" in {issue.code for issue in result.issues}


def test_cumulative_fuel_reserve_violation_is_reported(tmp_path, small_data_dir):
    routes, assignments = _write_q2_case(tmp_path, small_data_dir, fuel_violation=True)
    result = validate_solution("q2", routes, assignments, data_dir=small_data_dir)
    assert "FUEL_RESERVE_VIOLATION" in {issue.code for issue in result.issues}


def test_passenger_must_leave_at_first_destination_visit(tmp_path, small_data_dir):
    routes, assignments = _write_q2_case(tmp_path, small_data_dir, repeat_destination=True)
    result = validate_solution("q2", routes, assignments, data_dir=small_data_dir)
    assert "NOT_FIRST_DESTINATION_STOP" in {issue.code for issue in result.issues}
