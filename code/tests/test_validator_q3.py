from __future__ import annotations

from datetime import datetime

from src.io_utils import read_csv, write_csv
from src.validation import validate_solution


def _write_q3_case(tmp_path, data_dir, *, second_flight_gap: int | None = None, early_pickup: bool = False):
    people = [
        {
            "person_id": "E0001",
            "origin_id": "LAND",
            "destination_id": "F006",
            "earliest_pickup_time": "2026-08-03 07:00" if not early_pickup else "2026-08-03 08:30",
            "latest_arrival_time": "2026-08-03 10:00",
            "task_type": "emergency",
        },
        {
            "person_id": "E0002",
            "origin_id": "F006",
            "destination_id": "F020",
            "earliest_pickup_time": "2026-08-03 09:00",
            "latest_arrival_time": "2026-08-03 11:00",
            "task_type": "production",
        },
        {
            "person_id": "E0003",
            "origin_id": "F020",
            "destination_id": "LAND",
            "earliest_pickup_time": "2026-08-03 10:00",
            "latest_arrival_time": "2026-08-03 12:00",
            "task_type": "shift",
        },
        {
            "person_id": "E0004",
            "origin_id": "LAND",
            "destination_id": "F014",
            "earliest_pickup_time": "2026-08-03 06:00",
            "latest_arrival_time": "2026-08-04 18:00",
            "task_type": "temporary",
        },
    ]
    write_csv(
        data_dir / "peopleQ3.csv",
        ["person_id", "origin_id", "destination_id", "earliest_pickup_time", "latest_arrival_time", "task_type"],
        people,
    )
    routes = [
        {"aircraft_id": "A01-T2-H03", "flight_no": 1, "stop_order": 0, "facility_id": "A01", "arrival_time": "", "departure_time": "2026-08-03 08:00", "refuel": 0},
        {"aircraft_id": "A01-T2-H03", "flight_no": 1, "stop_order": 1, "facility_id": "F006", "arrival_time": "2026-08-03 09:05", "departure_time": "2026-08-03 09:25", "refuel": 1},
        {"aircraft_id": "A01-T2-H03", "flight_no": 1, "stop_order": 2, "facility_id": "F020", "arrival_time": "2026-08-03 10:11", "departure_time": "2026-08-03 10:21", "refuel": 0},
        {"aircraft_id": "A01-T2-H03", "flight_no": 1, "stop_order": 3, "facility_id": "A01", "arrival_time": "2026-08-03 11:08", "departure_time": "", "refuel": 0},
    ]
    assignments = [
        {"person_id": "E0001", "aircraft_id": "A01-T2-H03", "flight_no": 1, "pickup_stop_order": 0, "delivery_stop_order": 1},
        {"person_id": "E0002", "aircraft_id": "A01-T2-H03", "flight_no": 1, "pickup_stop_order": 1, "delivery_stop_order": 2},
        {"person_id": "E0003", "aircraft_id": "A01-T2-H03", "flight_no": 1, "pickup_stop_order": 2, "delivery_stop_order": 3},
        {"person_id": "E0004", "aircraft_id": "", "flight_no": "", "pickup_stop_order": "", "delivery_stop_order": ""},
    ]
    if second_flight_gap is not None:
        departure = datetime(2026, 8, 3, 11, 8).replace(minute=8 + second_flight_gap)
        # Use a short A01-F014-A01 T3 flight; exact times are constructed below.
        routes.extend(
            [
                {"aircraft_id": "A01-T2-H03", "flight_no": 2, "stop_order": 0, "facility_id": "A01", "arrival_time": "", "departure_time": departure.strftime("%Y-%m-%d %H:%M"), "refuel": 0},
                {"aircraft_id": "A01-T2-H03", "flight_no": 2, "stop_order": 1, "facility_id": "F014", "arrival_time": (departure.replace() + __import__('datetime').timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M"), "departure_time": (departure.replace() + __import__('datetime').timedelta(minutes=55)).strftime("%Y-%m-%d %H:%M"), "refuel": 0},
                {"aircraft_id": "A01-T2-H03", "flight_no": 2, "stop_order": 2, "facility_id": "A01", "arrival_time": (departure.replace() + __import__('datetime').timedelta(minutes=100)).strftime("%Y-%m-%d %H:%M"), "departure_time": "", "refuel": 0},
            ]
        )
    routes_path = tmp_path / "q3-routes.csv"
    assignments_path = tmp_path / "q3-assignments.csv"
    write_csv(routes_path, ["aircraft_id", "flight_no", "stop_order", "facility_id", "arrival_time", "departure_time", "refuel"], routes)
    write_csv(assignments_path, ["person_id", "aircraft_id", "flight_no", "pickup_stop_order", "delivery_stop_order"], assignments)
    return routes_path, assignments_path


def test_valid_q3_time_windows_and_optional_blank(tmp_path, small_data_dir):
    routes, assignments = _write_q3_case(tmp_path, small_data_dir)
    result = validate_solution("q3", routes, assignments, data_dir=small_data_dir)
    assert result.valid, [str(issue) for issue in result.issues]
    assert result.metrics is not None
    assert result.metrics.unserved_optional_passengers == 1
    assert result.metrics.total_aircraft_time_minutes == 188


def test_q3_earliest_pickup_violation(tmp_path, small_data_dir):
    routes, assignments = _write_q3_case(tmp_path, small_data_dir, early_pickup=True)
    result = validate_solution("q3", routes, assignments, data_dir=small_data_dir)
    assert "EARLIEST_PICKUP_VIOLATION" in {issue.code for issue in result.issues}


def test_q3_turnaround_boundary(tmp_path, small_data_dir):
    routes, assignments = _write_q3_case(tmp_path, small_data_dir, second_flight_gap=29)
    result = validate_solution("q3", routes, assignments, data_dir=small_data_dir)
    assert "TURNAROUND_VIOLATION" in {issue.code for issue in result.issues}


def test_q3_turnaround_exactly_30_minutes_is_legal(tmp_path, small_data_dir):
    routes, assignments = _write_q3_case(tmp_path, small_data_dir, second_flight_gap=30)
    result = validate_solution("q3", routes, assignments, data_dir=small_data_dir)
    assert result.valid, [str(issue) for issue in result.issues]


def test_q3_planning_horizon_is_enforced(tmp_path, small_data_dir):
    routes, assignments = _write_q3_case(tmp_path, small_data_dir)
    rows = read_csv(routes)
    for row in rows:
        for field in ("arrival_time", "departure_time"):
            if row[field]:
                timestamp = datetime.strptime(row[field], "%Y-%m-%d %H:%M")
                row[field] = timestamp.replace(day=10).strftime("%Y-%m-%d %H:%M")
    write_csv(routes, list(rows[0]), rows)
    result = validate_solution("q3", routes, assignments, data_dir=small_data_dir)
    assert "PLANNING_HORIZON_VIOLATION" in {issue.code for issue in result.issues}
