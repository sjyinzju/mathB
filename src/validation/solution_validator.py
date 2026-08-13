from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from ..config import ProblemConfig, ROOT, load_config
from ..data_pipeline import TIME_FORMAT, load_distance_matrix
from ..io_utils import csv_header, read_csv
from ..rules import EPSILON, flight_minutes, fuel_for_leg, minimum_stop_minutes


Q12_ROUTE_SCHEMA = ["aircraft_type", "flight_no", "stop_order", "facility_id", "refuel"]
Q12_ASSIGNMENT_SCHEMA = [
    "person_id",
    "aircraft_type",
    "flight_no",
    "pickup_stop_order",
    "delivery_stop_order",
]
Q3_ROUTE_SCHEMA = [
    "aircraft_id",
    "flight_no",
    "stop_order",
    "facility_id",
    "arrival_time",
    "departure_time",
    "refuel",
]
Q3_ASSIGNMENT_SCHEMA = [
    "person_id",
    "aircraft_id",
    "flight_no",
    "pickup_stop_order",
    "delivery_stop_order",
]


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    context: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class Metrics:
    total_aircraft_time_minutes: int
    total_passenger_travel_time_minutes: int
    total_flights: int
    total_fuel_consumption_kg: float
    seat_utilization: float
    served_passengers: int
    unserved_optional_passengers: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class ValidationResult:
    question: str
    valid: bool
    issues: list[ValidationIssue]
    metrics: Metrics | None

    def to_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "valid": self.valid,
            "issue_count": len(self.issues),
            "issues": [asdict(issue) for issue in self.issues],
            "metrics": self.metrics.to_dict() if self.metrics else None,
        }


@dataclass
class ParsedFlight:
    key: tuple[str, int]
    aircraft_type: str
    home_airport: str
    stops: list[dict[str, object]]
    arrivals: dict[int, int | datetime]
    departures: dict[int, int | datetime]
    fuel_consumption_kg: float
    aircraft_time_minutes: int
    seat_km_denominator: float = 0.0
    seat_km_numerator: float = 0.0


def _issue(issues: list[ValidationIssue], code: str, message: str, **context: object) -> None:
    issues.append(ValidationIssue(code=code, message=message, context=context))


def _parse_int(value: str, label: str, issues: list[ValidationIssue], context: dict[str, object]) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        _issue(issues, "INVALID_INTEGER", f"{label} must be an integer, got {value!r}", **context)
        return None
    return parsed


def _parse_refuel(value: str, issues: list[ValidationIssue], context: dict[str, object]) -> int | None:
    parsed = _parse_int(value, "refuel", issues, context)
    if parsed is not None and parsed not in {0, 1}:
        _issue(issues, "INVALID_REFUEL_FLAG", f"refuel must be 0 or 1, got {parsed}", **context)
        return None
    return parsed


def _parse_datetime(value: str, label: str, issues: list[ValidationIssue], context: dict[str, object]) -> datetime | None:
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except (TypeError, ValueError):
        _issue(issues, "INVALID_DATETIME", f"{label} must use YYYY-MM-DD HH:MM, got {value!r}", **context)
        return None


def _schema_check(path: Path, expected: list[str], issues: list[ValidationIssue], role: str) -> bool:
    actual = csv_header(path)
    if actual != expected:
        _issue(
            issues,
            "CSV_SCHEMA_MISMATCH",
            f"{role} header is {actual}, expected {expected}",
            path=str(path),
        )
        return False
    return True


def _group_routes(
    rows: list[dict[str, str]],
    question: str,
    config: ProblemConfig,
    issues: list[ValidationIssue],
) -> dict[tuple[str, int], list[dict[str, object]]]:
    groups: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    owner_field = "aircraft_id" if question == "q3" else "aircraft_type"
    for row_number, row in enumerate(rows, start=2):
        context = {"row": row_number}
        owner = row[owner_field]
        flight_no = _parse_int(row["flight_no"], "flight_no", issues, context)
        stop_order = _parse_int(row["stop_order"], "stop_order", issues, context)
        refuel = _parse_refuel(row["refuel"], issues, context)
        if flight_no is None or stop_order is None or refuel is None:
            continue
        if flight_no <= 0:
            _issue(issues, "INVALID_FLIGHT_NO", "flight_no must be positive", **context)
        if stop_order < 0:
            _issue(issues, "INVALID_STOP_ORDER", "stop_order must be non-negative", **context)
        if question == "q3":
            info = config.aircraft_home_and_type(owner)
            if info is None:
                _issue(issues, "INVALID_AIRCRAFT_ID", f"Unknown Q3 aircraft_id {owner}", **context)
        elif owner not in config.aircraft_types:
            _issue(issues, "INVALID_AIRCRAFT_TYPE", f"Unknown aircraft_type {owner}", **context)
        groups[(owner, flight_no)].append(
            {
                **row,
                "flight_no": flight_no,
                "stop_order": stop_order,
                "refuel": refuel,
                "_row": row_number,
            }
        )
    return groups


def _validate_numbering(
    groups: dict[tuple[str, int], list[dict[str, object]]],
    question: str,
    issues: list[ValidationIssue],
) -> None:
    by_owner: dict[str, list[int]] = defaultdict(list)
    for owner, flight_no in groups:
        by_owner[owner].append(flight_no)
    for owner, numbers in by_owner.items():
        unique = sorted(set(numbers))
        expected = list(range(1, len(unique) + 1))
        if unique != expected:
            _issue(
                issues,
                "NONCONTIGUOUS_FLIGHT_NO",
                f"{owner} flight numbers are {unique}, expected {expected}",
                owner=owner,
            )


def _validate_flight_structure(
    key: tuple[str, int],
    raw_stops: list[dict[str, object]],
    question: str,
    matrix: dict[str, dict[str, float]],
    config: ProblemConfig,
    issues: list[ValidationIssue],
) -> ParsedFlight | None:
    owner, flight_no = key
    stops = sorted(raw_stops, key=lambda row: int(row["stop_order"]))
    orders = [int(row["stop_order"]) for row in stops]
    expected_orders = list(range(len(stops)))
    if orders != expected_orders:
        _issue(
            issues,
            "NONCONTIGUOUS_STOP_ORDER",
            f"Flight {owner}/{flight_no} stop orders are {orders}, expected {expected_orders}",
            flight=str(key),
        )
        return None
    if len(stops) < 3:
        _issue(issues, "ROUTE_TOO_SHORT", f"Flight {owner}/{flight_no} must contain start, sea stop and return")
        return None
    locations = [str(row["facility_id"]) for row in stops]
    if any(node not in config.nodes for node in locations):
        unknown = sorted({node for node in locations if node not in config.nodes})
        _issue(issues, "UNKNOWN_ROUTE_NODE", f"Unknown route nodes: {unknown}", flight=str(key))
        return None
    home = locations[0]
    if home not in config.airports or locations[-1] != home:
        _issue(
            issues,
            "ROUTE_HOME_MISMATCH",
            f"Flight {owner}/{flight_no} must start and end at the same airport",
            first=home,
            last=locations[-1],
        )
    middle = locations[1:-1]
    other_airports = [node for node in middle if node in config.airports]
    if other_airports:
        _issue(issues, "MID_ROUTE_AIRPORT", f"Airports are not allowed between route endpoints: {other_airports}")
    if len(middle) > config.max_sea_landings:
        _issue(
            issues,
            "MAX_SEA_LANDINGS_EXCEEDED",
            f"Flight {owner}/{flight_no} has {len(middle)} sea landings > {config.max_sea_landings}",
        )
    if any(node not in config.facilities for node in middle):
        _issue(issues, "INVALID_SEA_STOP", "Every middle route record must be a sea facility", flight=str(key))
    if int(stops[0]["refuel"]) != 0 or int(stops[-1]["refuel"]) != 0:
        _issue(issues, "AIRPORT_REFUEL_FLAG", "Airport route rows must have refuel=0", flight=str(key))
    for stop in stops[1:-1]:
        if int(stop["refuel"]) == 1 and str(stop["facility_id"]) not in config.refuel_facilities:
            _issue(
                issues,
                "INVALID_REFUEL_LOCATION",
                f"Refuel requested at non-refuel facility {stop['facility_id']}",
                flight=str(key),
                stop_order=stop["stop_order"],
            )
    if question == "q3":
        info = config.aircraft_home_and_type(owner)
        if info is None:
            return None
        expected_home, aircraft_type = info
        if home != expected_home:
            _issue(
                issues,
                "AIRCRAFT_HOME_MISMATCH",
                f"{owner} belongs to {expected_home} but flight starts at {home}",
                flight=str(key),
            )
    else:
        aircraft_type = owner
        if aircraft_type not in config.aircraft_types:
            return None
    aircraft = config.aircraft_types[aircraft_type]

    fuel = aircraft.tank_capacity_kg
    total_fuel = 0.0
    arrivals: dict[int, int | datetime] = {0: 0}
    departures: dict[int, int | datetime] = {0: 0}
    q12_clock = 0
    start_time: datetime | None = None
    end_time: datetime | None = None

    if question == "q3":
        first = stops[0]
        last = stops[-1]
        if str(first["arrival_time"]).strip() or str(last["departure_time"]).strip():
            _issue(
                issues,
                "ENDPOINT_TIME_FIELDS",
                "First route row must have blank arrival and last row must have blank departure",
                flight=str(key),
            )
        if not str(first["departure_time"]).strip() or not str(last["arrival_time"]).strip():
            _issue(issues, "MISSING_ENDPOINT_TIME", "Q3 flight endpoints require departure/arrival timestamps", flight=str(key))
            return None
        start_time = _parse_datetime(str(first["departure_time"]), "departure_time", issues, {"flight": str(key)})
        end_time = _parse_datetime(str(last["arrival_time"]), "arrival_time", issues, {"flight": str(key)})
        if start_time is None or end_time is None:
            return None
        departures[0] = start_time
        if start_time < config.planning_start or end_time >= config.planning_end:
            _issue(
                issues,
                "PLANNING_HORIZON_VIOLATION",
                f"Flight interval [{start_time}, {end_time}] is outside "
                f"[{config.planning_start}, {config.planning_end})",
                flight=str(key),
            )
        if start_time.date() != end_time.date():
            _issue(issues, "SEA_OVERNIGHT", "A Q3 flight must return on its departure date", flight=str(key))
        if not (config.earliest_departure <= start_time.time() <= config.latest_departure):
            _issue(
                issues,
                "DEPARTURE_WINDOW_VIOLATION",
                f"Flight departs at {start_time}, outside 06:00-18:00",
                flight=str(key),
            )
        if end_time.time() > config.latest_return:
            _issue(issues, "LATE_RETURN", f"Flight returns at {end_time.time()}, after 20:00", flight=str(key))

    for index in range(len(stops) - 1):
        current = stops[index]
        next_stop = stops[index + 1]
        origin = str(current["facility_id"])
        destination = str(next_stop["facility_id"])
        distance = matrix[origin][destination]
        burned = fuel_for_leg(distance, aircraft)
        total_fuel += burned
        fuel -= burned
        if fuel + EPSILON < aircraft.reserve_kg:
            _issue(
                issues,
                "FUEL_RESERVE_VIOLATION",
                f"{aircraft_type} flight {flight_no} arrives {destination} with {fuel:.1f}kg < {aircraft.reserve_kg}kg",
                owner=owner,
                flight_no=flight_no,
                stop_order=index + 1,
            )
        minutes = flight_minutes(distance, aircraft.speed_kmh)
        if question == "q3":
            current_departure = departures.get(index)
            if not isinstance(current_departure, datetime):
                _issue(issues, "MISSING_DEPARTURE_TIME", f"Missing departure at stop {index}", flight=str(key))
                continue
            expected_arrival = current_departure + timedelta(minutes=minutes)
            raw_arrival = str(next_stop["arrival_time"])
            if not raw_arrival.strip():
                _issue(issues, "MISSING_ARRIVAL_TIME", f"Missing arrival at stop {index + 1}", flight=str(key))
                continue
            actual_arrival = _parse_datetime(raw_arrival, "arrival_time", issues, {"flight": str(key), "stop_order": index + 1})
            if actual_arrival is None:
                continue
            arrivals[index + 1] = actual_arrival
            if actual_arrival != expected_arrival:
                _issue(
                    issues,
                    "FLIGHT_TIME_MISMATCH",
                    f"Expected arrival {expected_arrival}, got {actual_arrival}",
                    flight=str(key),
                    leg=f"{origin}->{destination}",
                )
            if index + 1 < len(stops) - 1:
                raw_departure = str(next_stop["departure_time"])
                if not raw_departure.strip():
                    _issue(issues, "MISSING_DEPARTURE_TIME", f"Missing departure at stop {index + 1}", flight=str(key))
                else:
                    actual_departure = _parse_datetime(
                        raw_departure, "departure_time", issues, {"flight": str(key), "stop_order": index + 1}
                    )
                    if actual_departure is not None:
                        departures[index + 1] = actual_departure
                        try:
                            minimum = minimum_stop_minutes(
                                destination, bool(int(next_stop["refuel"])), config
                            )
                        except ValueError:
                            minimum = config.stop_without_refuel_minutes
                        dwell = round((actual_departure - actual_arrival).total_seconds() / 60)
                        if dwell < minimum:
                            _issue(
                                issues,
                                "STOP_TIME_VIOLATION",
                                f"Stop {destination} dwell is {dwell} minutes < {minimum}",
                                flight=str(key),
                                stop_order=index + 1,
                            )
        else:
            q12_clock += minutes
            arrivals[index + 1] = q12_clock
            if index + 1 < len(stops) - 1:
                try:
                    dwell = minimum_stop_minutes(destination, bool(int(next_stop["refuel"])), config)
                except ValueError:
                    dwell = config.stop_without_refuel_minutes
                q12_clock += dwell
                departures[index + 1] = q12_clock
        if int(next_stop["refuel"]) == 1:
            if destination in config.refuel_facilities:
                fuel = aircraft.tank_capacity_kg

    if question == "q3":
        arrivals[len(stops) - 1] = end_time  # type: ignore[assignment]
        aircraft_time = round((end_time - start_time).total_seconds() / 60)  # type: ignore[operator]
        if aircraft_time < 0:
            _issue(issues, "NEGATIVE_FLIGHT_DURATION", "Flight end precedes start", flight=str(key))
    else:
        aircraft_time = q12_clock
    return ParsedFlight(
        key=key,
        aircraft_type=aircraft_type,
        home_airport=home,
        stops=stops,
        arrivals=arrivals,
        departures=departures,
        fuel_consumption_kg=total_fuel,
        aircraft_time_minutes=aircraft_time,
    )


def _validate_q3_schedule(flights: dict[tuple[str, int], ParsedFlight], config: ProblemConfig, issues: list[ValidationIssue]) -> None:
    by_aircraft: dict[str, list[ParsedFlight]] = defaultdict(list)
    for (aircraft_id, _), flight in flights.items():
        by_aircraft[aircraft_id].append(flight)
    for aircraft_id, aircraft_flights in by_aircraft.items():
        aircraft_flights.sort(key=lambda flight: flight.departures[0])
        chronological_numbers = [flight.key[1] for flight in aircraft_flights]
        expected = list(range(1, len(aircraft_flights) + 1))
        if chronological_numbers != expected:
            _issue(
                issues,
                "Q3_FLIGHT_NO_TIME_ORDER",
                f"{aircraft_id} flight numbers by departure are {chronological_numbers}, expected {expected}",
            )
        for previous, current in zip(aircraft_flights, aircraft_flights[1:]):
            previous_arrival = previous.arrivals[len(previous.stops) - 1]
            current_departure = current.departures[0]
            if isinstance(previous_arrival, datetime) and isinstance(current_departure, datetime):
                gap = round((current_departure - previous_arrival).total_seconds() / 60)
                if gap < config.turnaround_minutes:
                    _issue(
                        issues,
                        "TURNAROUND_VIOLATION",
                        f"{aircraft_id} has {gap} minutes between flights, needs {config.turnaround_minutes}",
                        previous_flight=previous.key[1],
                        current_flight=current.key[1],
                    )


def _location_matches(expected: str, actual: str, home_airport: str) -> bool:
    return actual == (home_airport if expected == "LAND" else expected)


def _validate_assignments_and_loads(
    assignment_rows: list[dict[str, str]],
    demand_rows: list[dict[str, str]],
    flights: dict[tuple[str, int], ParsedFlight],
    question: str,
    matrix: dict[str, dict[str, float]],
    config: ProblemConfig,
    issues: list[ValidationIssue],
) -> tuple[int, int, float, float, int]:
    owner_field = "aircraft_id" if question == "q3" else "aircraft_type"
    demands = {row["person_id"]: row for row in demand_rows}
    row_counts = Counter(row["person_id"] for row in assignment_rows)
    for person_id, count in row_counts.items():
        if count != 1:
            _issue(issues, "DUPLICATE_ASSIGNMENT_ROW", f"{person_id} appears {count} times in assignments")
    unknown_people = sorted(set(row_counts) - set(demands))
    if unknown_people:
        _issue(issues, "UNKNOWN_ASSIGNED_PERSON", f"Assignments contain unknown people: {unknown_people[:10]}")
    missing_rows = sorted(set(demands) - set(row_counts))
    if missing_rows:
        _issue(issues, "MISSING_ASSIGNMENT_ROWS", f"Assignments omit {len(missing_rows)} demand rows", examples=missing_rows[:10])

    assignments_by_flight: dict[tuple[str, int], list[tuple[str, int, int]]] = defaultdict(list)
    passenger_time = 0
    served = 0
    unserved_optional = 0
    for row_number, row in enumerate(assignment_rows, start=2):
        person_id = row["person_id"]
        if person_id not in demands:
            continue
        demand = demands[person_id]
        schedule_fields = [row[owner_field], row["flight_no"], row["pickup_stop_order"], row["delivery_stop_order"]]
        blanks = [not value.strip() for value in schedule_fields]
        if any(blanks):
            if question == "q3" and all(blanks) and demand["task_type"] in config.optional_task_types:
                unserved_optional += 1
                continue
            code = "PARTIAL_ASSIGNMENT" if not all(blanks) else "MANDATORY_PERSON_UNASSIGNED"
            _issue(issues, code, f"{person_id} has invalid blank assignment fields", row=row_number)
            continue
        owner = row[owner_field]
        flight_no = _parse_int(row["flight_no"], "flight_no", issues, {"row": row_number, "person_id": person_id})
        pickup = _parse_int(row["pickup_stop_order"], "pickup_stop_order", issues, {"row": row_number, "person_id": person_id})
        delivery = _parse_int(row["delivery_stop_order"], "delivery_stop_order", issues, {"row": row_number, "person_id": person_id})
        if flight_no is None or pickup is None or delivery is None:
            continue
        key = (owner, flight_no)
        flight = flights.get(key)
        if flight is None:
            _issue(issues, "UNKNOWN_ASSIGNED_FLIGHT", f"{person_id} references missing flight {key}")
            continue
        if not (0 <= pickup < delivery < len(flight.stops)):
            _issue(
                issues,
                "PICKUP_DELIVERY_ORDER",
                f"{person_id} requires 0 <= pickup < delivery < {len(flight.stops)}, got {pickup}/{delivery}",
            )
            continue
        pickup_node = str(flight.stops[pickup]["facility_id"])
        delivery_node = str(flight.stops[delivery]["facility_id"])
        if not _location_matches(demand["origin_id"], pickup_node, flight.home_airport):
            _issue(
                issues,
                "PICKUP_NODE_MISMATCH",
                f"{person_id} origin {demand['origin_id']} does not match route node {pickup_node}",
            )
        if not _location_matches(demand["destination_id"], delivery_node, flight.home_airport):
            _issue(
                issues,
                "DELIVERY_NODE_MISMATCH",
                f"{person_id} destination {demand['destination_id']} does not match route node {delivery_node}",
            )
        expected_destination = flight.home_airport if demand["destination_id"] == "LAND" else demand["destination_id"]
        first_destination = next(
            (index for index in range(pickup + 1, len(flight.stops)) if flight.stops[index]["facility_id"] == expected_destination),
            None,
        )
        if first_destination != delivery:
            _issue(
                issues,
                "NOT_FIRST_DESTINATION_STOP",
                f"{person_id} must leave at first destination visit {first_destination}, assignment says {delivery}",
            )
        if question == "q3":
            departure = flight.departures.get(pickup)
            arrival = flight.arrivals.get(delivery)
            earliest = datetime.strptime(demand["earliest_pickup_time"], TIME_FORMAT)
            latest = datetime.strptime(demand["latest_arrival_time"], TIME_FORMAT)
            if isinstance(departure, datetime) and departure < earliest:
                _issue(
                    issues,
                    "EARLIEST_PICKUP_VIOLATION",
                    f"{person_id} leaves at {departure}, before {earliest}",
                )
            if isinstance(arrival, datetime) and arrival > latest:
                _issue(
                    issues,
                    "LATEST_ARRIVAL_VIOLATION",
                    f"{person_id} arrives at {arrival}, after {latest}",
                )
        departure = flight.departures.get(pickup)
        arrival = flight.arrivals.get(delivery)
        if isinstance(departure, datetime) and isinstance(arrival, datetime):
            passenger_time += round((arrival - departure).total_seconds() / 60)
        elif isinstance(departure, int) and isinstance(arrival, int):
            passenger_time += arrival - departure
        assignments_by_flight[key].append((person_id, pickup, delivery))
        served += 1

    total_numerator = 0.0
    total_denominator = 0.0
    for key, flight in flights.items():
        pickups: Counter[int] = Counter()
        deliveries: Counter[int] = Counter()
        for _, pickup, delivery in assignments_by_flight.get(key, []):
            pickups[pickup] += 1
            deliveries[delivery] += 1
        load = 0
        capacity = config.aircraft_types[flight.aircraft_type].seats
        for index in range(len(flight.stops)):
            load -= deliveries[index]
            if load < 0:
                _issue(issues, "NEGATIVE_PASSENGER_LOAD", f"Flight {key} has negative load at stop {index}")
                load = 0
            load += pickups[index]
            if load > capacity:
                _issue(
                    issues,
                    "CAPACITY_VIOLATION",
                    f"Flight {key} leaves stop {index} with {load} passengers > {capacity}",
                    stop_order=index,
                )
            if index < len(flight.stops) - 1:
                origin = str(flight.stops[index]["facility_id"])
                destination = str(flight.stops[index + 1]["facility_id"])
                distance = matrix[origin][destination]
                total_numerator += load * distance
                total_denominator += capacity * distance
        if load != 0:
            _issue(issues, "PASSENGERS_REMAIN_ON_BOARD", f"Flight {key} ends with {load} passengers")
    return passenger_time, served, total_numerator, total_denominator, unserved_optional


def validate_solution(
    question: str,
    routes_path: Path | str,
    assignments_path: Path | str,
    *,
    data_dir: Path | str | None = None,
    config: ProblemConfig | None = None,
) -> ValidationResult:
    question = question.lower()
    if question not in {"q1", "q2", "q3"}:
        raise ValueError("question must be q1, q2 or q3")
    config = config or load_config()
    routes_path = Path(routes_path)
    assignments_path = Path(assignments_path)
    data_dir = Path(data_dir) if data_dir else ROOT / "data" / "raw"
    issues: list[ValidationIssue] = []
    route_schema = Q3_ROUTE_SCHEMA if question == "q3" else Q12_ROUTE_SCHEMA
    assignment_schema = Q3_ASSIGNMENT_SCHEMA if question == "q3" else Q12_ASSIGNMENT_SCHEMA
    if not _schema_check(routes_path, route_schema, issues, "routes"):
        return ValidationResult(question, False, issues, None)
    if not _schema_check(assignments_path, assignment_schema, issues, "assignments"):
        return ValidationResult(question, False, issues, None)

    route_rows = read_csv(routes_path)
    assignment_rows = read_csv(assignments_path)
    demand_rows = read_csv(data_dir / f"people{question.upper()}.csv")
    _, matrix = load_distance_matrix(data_dir / "distances.csv")
    groups = _group_routes(route_rows, question, config, issues)
    _validate_numbering(groups, question, issues)
    parsed_flights: dict[tuple[str, int], ParsedFlight] = {}
    for key, stops in groups.items():
        parsed = _validate_flight_structure(key, stops, question, matrix, config, issues)
        if parsed is not None:
            parsed_flights[key] = parsed
    if question == "q3":
        _validate_q3_schedule(parsed_flights, config, issues)

    passenger_time, served, numerator, denominator, unserved_optional = _validate_assignments_and_loads(
        assignment_rows,
        demand_rows,
        parsed_flights,
        question,
        matrix,
        config,
        issues,
    )
    metrics = Metrics(
        total_aircraft_time_minutes=sum(flight.aircraft_time_minutes for flight in parsed_flights.values()),
        total_passenger_travel_time_minutes=passenger_time,
        total_flights=len(parsed_flights),
        total_fuel_consumption_kg=round(sum(flight.fuel_consumption_kg for flight in parsed_flights.values()), 6),
        seat_utilization=(numerator / denominator if denominator else 0.0),
        served_passengers=served,
        unserved_optional_passengers=unserved_optional,
    )
    return ValidationResult(question=question, valid=not issues, issues=issues, metrics=metrics)
