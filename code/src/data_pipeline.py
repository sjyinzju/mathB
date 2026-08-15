from __future__ import annotations

import csv
import json
import math
import shutil
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .config import ProblemConfig, ROOT, load_config
from .io_utils import csv_header, read_csv, sha256, write_csv, write_json
from .rules import EPSILON, flight_minutes, fuel_for_leg


INPUT_NAMES = ("distances.csv", "peopleQ1.csv", "peopleQ2.csv", "peopleQ3.csv")
TIME_FORMAT = "%Y-%m-%d %H:%M"


@dataclass(frozen=True)
class DataPaths:
    root: Path
    source_dir: Path
    raw_dir: Path
    processed_dir: Path
    eda_dir: Path


def discover_source_dir(root: Path = ROOT) -> Path:
    candidates: list[Path] = []
    for path in root.rglob("distances.csv"):
        parent = path.parent
        if parent == root / "data" / "raw":
            continue
        if all((parent / name).exists() for name in INPUT_NAMES):
            candidates.append(parent)
    if not candidates:
        raise FileNotFoundError("Could not locate the directory containing the four official input CSV files")
    candidates.sort(key=lambda p: ("附件" not in str(p), len(p.parts), str(p)))
    return candidates[0]


def default_paths(root: Path = ROOT) -> DataPaths:
    return DataPaths(
        root=root,
        source_dir=discover_source_dir(root),
        raw_dir=root / "data" / "raw",
        processed_dir=root / "data" / "processed",
        eda_dir=root / "outputs" / "eda",
    )


def protect_raw_inputs(paths: DataPaths) -> list[dict[str, object]]:
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    for name in INPUT_NAMES:
        source = paths.source_dir / name
        target = paths.raw_dir / name
        source_hash = sha256(source)
        if target.exists() and sha256(target) != source_hash:
            raise ValueError(f"Protected raw copy differs from official attachment: {target}")
        if not target.exists():
            shutil.copy2(source, target)
        target_hash = sha256(target)
        if target_hash != source_hash:
            raise IOError(f"Raw copy verification failed for {name}")
        manifest.append(
            {
                "file": name,
                "source_path": str(source.relative_to(paths.root)),
                "raw_path": str(target.relative_to(paths.root)),
                "size_bytes": target.stat().st_size,
                "sha256": target_hash,
            }
        )
    write_json(paths.raw_dir / "manifest.json", {"files": manifest})
    return manifest


def load_distance_matrix(path: Path) -> tuple[list[str], dict[str, dict[str, float]]]:
    rows = read_csv(path)
    if not rows:
        raise ValueError("Distance matrix is empty")
    nodes = list(rows[0].keys())[1:]
    matrix: dict[str, dict[str, float]] = {}
    for row in rows:
        origin = row["from_id"]
        matrix[origin] = {}
        for destination in nodes:
            raw = row[destination]
            if raw.strip() == "":
                raise ValueError(f"Missing distance at {origin}->{destination}")
            matrix[origin][destination] = float(raw)
    return nodes, matrix


def node_kind(node: str, config: ProblemConfig) -> str:
    if node == "LAND":
        return "LAND"
    if node in config.airports:
        return "AIRPORT"
    if node in config.facilities:
        return "FACILITY"
    return "UNKNOWN"


def demand_direction(origin: str, destination: str, config: ProblemConfig) -> str:
    origin_kind = node_kind(origin, config)
    destination_kind = node_kind(destination, config)
    if origin_kind in {"LAND", "AIRPORT"} and destination_kind == "FACILITY":
        return "outbound"
    if origin_kind == "FACILITY" and destination_kind in {"LAND", "AIRPORT"}:
        return "inbound"
    if origin_kind == destination_kind == "FACILITY":
        return "shuttle"
    return "invalid"


def analyze_triangle_inequality(nodes: list[str], matrix: dict[str, dict[str, float]]) -> dict[str, object]:
    unordered_pairs: set[tuple[str, str]] = set()
    directed_pairs: set[tuple[str, str]] = set()
    triples: list[dict[str, object]] = []
    for origin in nodes:
        for destination in nodes:
            if origin == destination:
                continue
            direct = matrix[origin][destination]
            for middle in nodes:
                if middle in {origin, destination}:
                    continue
                via = matrix[origin][middle] + matrix[middle][destination]
                if direct > via + EPSILON:
                    directed_pairs.add((origin, destination))
                    unordered_pairs.add(tuple(sorted((origin, destination))))
                    triples.append(
                        {
                            "origin": origin,
                            "destination": destination,
                            "middle": middle,
                            "direct_distance_km": direct,
                            "via_distance_km": via,
                            "saving_km": direct - via,
                        }
                    )
    triples.sort(key=lambda row: (-float(row["saving_km"]), str(row["origin"]), str(row["destination"]), str(row["middle"])))
    return {
        "unordered_pair_count": len(unordered_pairs),
        "directed_pair_count": len(directed_pairs),
        "violating_triple_count": len(triples),
        "top_violations": triples[:20],
    }


def validate_distance_data(
    path: Path,
    config: ProblemConfig,
    demand_nodes: set[str],
) -> tuple[dict[str, object], list[str], dict[str, dict[str, float]]]:
    header = csv_header(path)
    rows = read_csv(path)
    row_nodes = [row["from_id"] for row in rows]
    column_nodes = header[1:]
    numeric_values: list[float] = []
    missing_cells: list[str] = []
    negative_cells: list[str] = []
    matrix: dict[str, dict[str, float]] = {}
    for row in rows:
        origin = row["from_id"]
        matrix[origin] = {}
        for destination in column_nodes:
            value = row.get(destination, "")
            if value.strip() == "":
                missing_cells.append(f"{origin}->{destination}")
                continue
            number = float(value)
            matrix[origin][destination] = number
            numeric_values.append(number)
            if number < 0:
                negative_cells.append(f"{origin}->{destination}")
    expected = set(config.nodes)
    same_nodes = row_nodes == column_nodes
    symmetry_errors: list[str] = []
    diagonal_errors: list[str] = []
    if not missing_cells and set(row_nodes) == set(column_nodes):
        for node in row_nodes:
            if abs(matrix[node][node]) > EPSILON:
                diagonal_errors.append(node)
        for i, origin in enumerate(row_nodes):
            for destination in row_nodes[i + 1 :]:
                if abs(matrix[origin][destination] - matrix[destination][origin]) > EPSILON:
                    symmetry_errors.append(f"{origin}<->{destination}")
    off_diagonal = [matrix[a][b] for a in row_nodes for b in column_nodes if a != b and b in matrix.get(a, {})]
    triangle = analyze_triangle_inequality(row_nodes, matrix) if not missing_cells and same_nodes else {
        "unordered_pair_count": None,
        "directed_pair_count": None,
        "violating_triple_count": None,
        "top_violations": [],
    }
    report = {
        "file": path.name,
        "row_count": len(rows),
        "column_node_count": len(column_nodes),
        "row_column_node_order_identical": same_nodes,
        "configured_node_set_matches": set(row_nodes) == set(column_nodes) == expected,
        "airport_count": sum(node in config.airports for node in row_nodes),
        "facility_count": sum(node in config.facilities for node in row_nodes),
        "missing_cell_count": len(missing_cells),
        "negative_distance_count": len(negative_cells),
        "zero_diagonal": not diagonal_errors,
        "symmetric": not symmetry_errors,
        "min_off_diagonal_km": min(off_diagonal) if off_diagonal else None,
        "max_off_diagonal_km": max(off_diagonal) if off_diagonal else None,
        "all_demand_nodes_present": demand_nodes <= expected,
        "unknown_demand_nodes": sorted(demand_nodes - expected),
        "triangle_inequality": triangle,
        "passed": all(
            [
                len(rows) == 55,
                len(column_nodes) == 55,
                same_nodes,
                set(row_nodes) == expected,
                not missing_cells,
                not negative_cells,
                not diagonal_errors,
                not symmetry_errors,
                demand_nodes <= expected,
            ]
        ),
    }
    return report, row_nodes, matrix


def validate_people_file(path: Path, question: str, config: ProblemConfig) -> dict[str, object]:
    rows = read_csv(path)
    ids = [row["person_id"] for row in rows]
    missing_by_column = {column: sum(not row[column].strip() for row in rows) for column in rows[0]}
    unknown_nodes: set[str] = set()
    invalid_land: list[str] = []
    invalid_airport_usage: list[str] = []
    self_trips: list[str] = []
    invalid_directions: list[str] = []
    directions = Counter()
    for row in rows:
        origin = row["origin_id"]
        destination = row["destination_id"]
        if origin == destination:
            self_trips.append(row["person_id"])
        for node in (origin, destination):
            if node != "LAND" and node not in config.nodes:
                unknown_nodes.add(node)
        direction = demand_direction(origin, destination, config)
        directions[direction] += 1
        if direction == "invalid":
            invalid_directions.append(row["person_id"])
        if origin == "LAND" and destination not in config.facilities:
            invalid_land.append(row["person_id"])
        if destination == "LAND" and origin not in config.facilities:
            invalid_land.append(row["person_id"])
        if origin in config.airports and destination not in config.facilities:
            invalid_airport_usage.append(row["person_id"])
        if destination in config.airports and origin not in config.facilities:
            invalid_airport_usage.append(row["person_id"])
    report: dict[str, object] = {
        "file": path.name,
        "row_count": len(rows),
        "unique_person_count": len(set(ids)),
        "duplicate_person_id_count": len(ids) - len(set(ids)),
        "missing_by_column": missing_by_column,
        "unknown_nodes": sorted(unknown_nodes),
        "self_trip_count": len(self_trips),
        "invalid_land_usage_count": len(set(invalid_land)),
        "invalid_specific_airport_usage_count": len(set(invalid_airport_usage)),
        "invalid_direction_count": len(invalid_directions),
        "direction_counts": dict(sorted(directions.items())),
    }
    if question == "q3":
        task_counts = Counter()
        invalid_times: list[str] = []
        invalid_tasks: list[str] = []
        outside_horizon: list[str] = []
        windows: list[int] = []
        for row in rows:
            task = row["task_type"]
            task_counts[task] += 1
            if task not in config.task_priority:
                invalid_tasks.append(row["person_id"])
            try:
                earliest = datetime.strptime(row["earliest_pickup_time"], TIME_FORMAT)
                latest = datetime.strptime(row["latest_arrival_time"], TIME_FORMAT)
            except ValueError:
                invalid_times.append(row["person_id"])
                continue
            if earliest >= latest:
                invalid_times.append(row["person_id"])
            else:
                windows.append(round((latest - earliest).total_seconds() / 60))
            if earliest < config.planning_start or latest > config.planning_end:
                outside_horizon.append(row["person_id"])
        report.update(
            {
                "task_type_counts": dict(sorted(task_counts.items())),
                "invalid_time_window_count": len(invalid_times),
                "invalid_task_type_count": len(invalid_tasks),
                "outside_planning_horizon_count": len(outside_horizon),
                "window_minutes_min": min(windows) if windows else None,
                "window_minutes_max": max(windows) if windows else None,
            }
        )
    report["passed"] = all(
        [
            report["duplicate_person_id_count"] == 0,
            not any(missing_by_column.values()),
            not unknown_nodes,
            not self_trips,
            not invalid_land,
            not invalid_airport_usage,
            not invalid_directions,
            question != "q3"
            or (
                report["invalid_time_window_count"] == 0
                and report["invalid_task_type_count"] == 0
                and report["outside_planning_horizon_count"] == 0
            ),
        ]
    )
    return report


def cross_file_checks(raw_dir: Path) -> dict[str, object]:
    q1 = {row["person_id"]: row for row in read_csv(raw_dir / "peopleQ1.csv")}
    q2 = {row["person_id"]: row for row in read_csv(raw_dir / "peopleQ2.csv")}
    q3 = {row["person_id"]: row for row in read_csv(raw_dir / "peopleQ3.csv")}
    q1_subset = set(q1) <= set(q2)
    q1_matches = q1_subset and all(
        q1[person]["origin_id"] == q2[person]["origin_id"]
        and q1[person]["destination_id"] == q2[person]["destination_id"]
        for person in q1
    )
    return {
        "q1_ids_subset_of_q2": q1_subset,
        "q1_od_matches_q2": q1_matches,
        "q2_q3_person_ids_equal": set(q2) == set(q3),
        "q2_q3_od_match": set(q2) == set(q3)
        and all(
            q2[person]["origin_id"] == q3[person]["origin_id"]
            and q2[person]["destination_id"] == q3[person]["destination_id"]
            for person in q2
        ),
    }


def nearest_airport_features(
    facility: str,
    matrix: dict[str, dict[str, float]],
    config: ProblemConfig,
) -> tuple[str, float, str]:
    ranking = sorted((matrix[airport][facility], airport) for airport in config.airports)
    return ranking[0][1], ranking[0][0], json.dumps([airport for _, airport in ranking], ensure_ascii=False)


def build_nodes(config: ProblemConfig) -> list[dict[str, object]]:
    return [
        {
            "node_id": node,
            "node_type": "AIRPORT" if node in config.airports else "FACILITY",
            "is_airport": int(node in config.airports),
            "is_facility": int(node in config.facilities),
            "can_refuel": int(node in config.refuel_facilities),
        }
        for node in config.nodes
    ]


def build_aircraft_types(config: ProblemConfig) -> list[dict[str, object]]:
    return [
        {
            "aircraft_type": aircraft.aircraft_type,
            "seats": aircraft.seats,
            "speed_kmh": aircraft.speed_kmh,
            "burn_kg_per_km": aircraft.burn_kg_per_km,
            "tank_capacity_kg": aircraft.tank_capacity_kg,
            "reserve_kg": aircraft.reserve_kg,
            "usable_fuel_kg": aircraft.usable_fuel_kg,
            "full_tank_usable_distance_km": round(aircraft.full_tank_usable_distance_km, 6),
        }
        for aircraft in config.aircraft_types.values()
    ]


def _candidate_airports(node: str, side: str, config: ProblemConfig) -> tuple[str, str]:
    if node == "LAND":
        return json.dumps(list(config.airports)), ""
    if node in config.airports:
        return json.dumps([node]), node
    if node in config.facilities:
        return json.dumps([]), ""
    raise ValueError(f"Unknown {side} node: {node}")


def _direct_lower_bound(
    origin: str,
    destination: str,
    matrix: dict[str, dict[str, float]],
    config: ProblemConfig,
) -> tuple[float, str, str]:
    origins = config.airports if origin == "LAND" else (origin,)
    destinations = config.airports if destination == "LAND" else (destination,)
    candidates = [(matrix[a][b], a, b) for a in origins for b in destinations]
    return min(candidates)


def _technical_time_lower_bound(
    origin: str,
    destination: str,
    matrix: dict[str, dict[str, float]],
    config: ProblemConfig,
) -> tuple[int, str, str, str, int, float]:
    origins = config.airports if origin == "LAND" else (origin,)
    destinations = config.airports if destination == "LAND" else (destination,)
    best: tuple[int, str, str, str, int, float] | None = None
    refuel_nodes = set(config.refuel_facilities)
    for aircraft_type, aircraft in config.aircraft_types.items():
        labels: dict[str, list[tuple[int, float, int]]] = defaultdict(list)
        queue: deque[tuple[str, float, int, int]] = deque()
        for actual_origin in origins:
            start_fuel = aircraft.tank_capacity_kg
            labels[actual_origin].append((0, start_fuel, 0))
            queue.append((actual_origin, start_fuel, 0, 0))
        while queue:
            node, fuel, minutes_used, sea_stops = queue.popleft()
            if sea_stops >= config.max_sea_landings:
                continue
            for next_node in config.facilities:
                if next_node == node:
                    continue
                leg_fuel = fuel_for_leg(matrix[node][next_node], aircraft)
                arrival_fuel = fuel - leg_fuel
                if arrival_fuel + EPSILON < aircraft.reserve_kg:
                    continue
                leg_minutes = flight_minutes(matrix[node][next_node], aircraft.speed_kmh)
                if next_node in destinations:
                    candidate = (
                        minutes_used + leg_minutes,
                        aircraft_type,
                        next(iter(origins)) if len(origins) == 1 else "flexible",
                        next_node,
                        sea_stops + 1,
                        aircraft.tank_capacity_kg - arrival_fuel,
                    )
                    if best is None or candidate[0] < best[0]:
                        best = candidate
                for refuel in (False, True) if next_node in refuel_nodes else (False,):
                    departure_fuel = aircraft.tank_capacity_kg if refuel else arrival_fuel
                    stop_minutes = config.stop_with_refuel_minutes if refuel else config.stop_without_refuel_minutes
                    new_minutes = minutes_used + leg_minutes + stop_minutes
                    new_stops = sea_stops + 1
                    dominated = any(
                        old_minutes <= new_minutes and old_fuel + EPSILON >= departure_fuel and old_stops <= new_stops
                        for old_minutes, old_fuel, old_stops in labels[next_node]
                    )
                    if dominated:
                        continue
                    labels[next_node] = [
                        label
                        for label in labels[next_node]
                        if not (
                            new_minutes <= label[0]
                            and departure_fuel + EPSILON >= label[1]
                            and new_stops <= label[2]
                        )
                    ]
                    labels[next_node].append((new_minutes, departure_fuel, new_stops))
                    queue.append((next_node, departure_fuel, new_minutes, new_stops))
            for actual_destination in destinations:
                if actual_destination == node:
                    continue
                arrival_fuel = fuel - fuel_for_leg(matrix[node][actual_destination], aircraft)
                if arrival_fuel + EPSILON < aircraft.reserve_kg:
                    continue
                new_minutes = minutes_used + flight_minutes(matrix[node][actual_destination], aircraft.speed_kmh)
                candidate = (
                    new_minutes,
                    aircraft_type,
                    next(iter(origins)) if len(origins) == 1 else "flexible",
                    actual_destination,
                    sea_stops,
                    aircraft.tank_capacity_kg - arrival_fuel,
                )
                if best is None or candidate[0] < best[0]:
                    best = candidate
    if best is None:
        raise ValueError(f"No technical passenger path lower bound found for {origin}->{destination}")
    return best


def _minimum_closed_route_stops(
    airport: str,
    target: str,
    aircraft_type: str,
    matrix: dict[str, dict[str, float]],
    config: ProblemConfig,
    *,
    allow_refuel: bool,
) -> int | None:
    """Minimum sea stops for any airport-closed route visiting target, capped at the official limit."""
    aircraft = config.aircraft_types[aircraft_type]
    # State is (current sea node, target_seen, refuel_used) -> best departure fuel.
    states: dict[tuple[str, bool, bool], float] = {}
    for stop_count in range(1, config.max_sea_landings + 1):
        candidates: dict[tuple[str, bool, bool], float] = {}
        previous = {(airport, False, False): aircraft.tank_capacity_kg} if stop_count == 1 else states
        for (current, seen, refueled), fuel in previous.items():
            for next_node in config.facilities:
                if next_node == current:
                    continue
                arrival = fuel - fuel_for_leg(matrix[current][next_node], aircraft)
                if arrival + EPSILON < aircraft.reserve_kg:
                    continue
                next_seen = seen or next_node == target
                options = [(arrival, refueled)]
                if allow_refuel and next_node in config.refuel_facilities:
                    options.append((aircraft.tank_capacity_kg, True))
                for departure_fuel, next_refueled in options:
                    key = (next_node, next_seen, next_refueled)
                    candidates[key] = max(candidates.get(key, -math.inf), departure_fuel)
        states = candidates
        for (current, seen, _), fuel in states.items():
            if seen and fuel - fuel_for_leg(matrix[current][airport], aircraft) + EPSILON >= aircraft.reserve_kg:
                return stop_count
    return None


def _closed_route_reachability(
    matrix: dict[str, dict[str, float]], config: ProblemConfig
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for aircraft_type in config.aircraft_types:
        for airport in config.airports:
            for facility in config.facilities:
                with_refuel = _minimum_closed_route_stops(
                    airport, facility, aircraft_type, matrix, config, allow_refuel=True
                )
                without_refuel = _minimum_closed_route_stops(
                    airport, facility, aircraft_type, matrix, config, allow_refuel=False
                )
                rows.append(
                    {
                        "aircraft_type": aircraft_type,
                        "airport": airport,
                        "facility": facility,
                        "closed_route_feasible_within_5_stops": int(with_refuel is not None),
                        "minimum_sea_stops_with_refuel_allowed": "" if with_refuel is None else with_refuel,
                        "closed_route_feasible_without_refuel": int(without_refuel is not None),
                        "minimum_sea_stops_without_refuel": "" if without_refuel is None else without_refuel,
                        "refuel_required_for_closed_route": int(with_refuel is not None and without_refuel is None),
                    }
                )
    return rows


def _refuel_hub_summary(
    matrix: dict[str, dict[str, float]], config: ProblemConfig
) -> list[dict[str, object]]:
    """Count targets supportable by a concrete two-sea-stop closed route containing each refuel hub."""
    rows: list[dict[str, object]] = []
    for aircraft_type, aircraft in config.aircraft_types.items():
        for airport in config.airports:
            for hub in sorted(config.refuel_facilities):
                supported: set[str] = set()
                for target in config.facilities:
                    if target == hub:
                        continue
                    # airport -> hub (refuel) -> target -> airport
                    arrival_hub = aircraft.tank_capacity_kg - fuel_for_leg(matrix[airport][hub], aircraft)
                    if arrival_hub + EPSILON >= aircraft.reserve_kg:
                        after_target = aircraft.tank_capacity_kg - fuel_for_leg(matrix[hub][target], aircraft)
                        after_home = after_target - fuel_for_leg(matrix[target][airport], aircraft)
                        if after_target + EPSILON >= aircraft.reserve_kg and after_home + EPSILON >= aircraft.reserve_kg:
                            supported.add(target)
                    # airport -> target -> hub (refuel) -> airport
                    after_target = aircraft.tank_capacity_kg - fuel_for_leg(matrix[airport][target], aircraft)
                    arrival_hub = after_target - fuel_for_leg(matrix[target][hub], aircraft)
                    after_home = aircraft.tank_capacity_kg - fuel_for_leg(matrix[hub][airport], aircraft)
                    if (
                        after_target + EPSILON >= aircraft.reserve_kg
                        and arrival_hub + EPSILON >= aircraft.reserve_kg
                        and after_home + EPSILON >= aircraft.reserve_kg
                    ):
                        supported.add(target)
                rows.append(
                    {
                        "aircraft_type": aircraft_type,
                        "airport": airport,
                        "refuel_facility": hub,
                        "two_stop_supported_target_count": len(supported),
                        "supported_targets": "|".join(sorted(supported)),
                    }
                )
    return rows


def build_demands(
    raw_rows: list[dict[str, str]],
    question: str,
    matrix: dict[str, dict[str, float]],
    config: ProblemConfig,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    technical_cache: dict[tuple[str, str], tuple[int, str, str, str, int, float]] = {}
    for source in raw_rows:
        origin = source["origin_id"]
        destination = source["destination_id"]
        origin_candidates, fixed_origin = _candidate_airports(origin, "origin", config)
        destination_candidates, fixed_destination = _candidate_airports(destination, "destination", config)
        sea_endpoint = destination if destination in config.facilities else origin
        nearest, nearest_distance, rankings = nearest_airport_features(sea_endpoint, matrix, config)
        direct_distance, best_direct_origin, best_direct_destination = _direct_lower_bound(origin, destination, matrix, config)
        key = (origin, destination)
        if key not in technical_cache:
            technical_cache[key] = _technical_time_lower_bound(origin, destination, matrix, config)
        lower_minutes, lower_type, _, lower_destination, lower_stops, lower_fuel = technical_cache[key]
        row: dict[str, object] = {
            "person_id": source["person_id"],
            "origin": origin,
            "destination": destination,
            "origin_type": node_kind(origin, config),
            "destination_type": node_kind(destination, config),
            "direction": demand_direction(origin, destination, config),
            "origin_is_flexible_land": int(origin == "LAND"),
            "destination_is_flexible_land": int(destination == "LAND"),
            "candidate_origin_airports": origin_candidates,
            "candidate_destination_airports": destination_candidates,
            "fixed_origin_airport": fixed_origin,
            "fixed_destination_airport": fixed_destination,
            "nearest_airport": nearest,
            "nearest_airport_distance_km": nearest_distance,
            "airport_rankings": rankings,
            "direct_distance_lower_bound_km": direct_distance,
            "direct_lower_bound_origin": best_direct_origin,
            "direct_lower_bound_destination": best_direct_destination,
            "technical_min_travel_minutes_lower_bound": lower_minutes,
            "technical_min_aircraft_type": lower_type,
            "technical_min_sea_stops": lower_stops,
            "minimum_fuel_requirement_estimate_kg": round(
                direct_distance * min(aircraft.burn_kg_per_km for aircraft in config.aircraft_types.values()), 6
            ),
        }
        if question == "q3":
            earliest = datetime.strptime(source["earliest_pickup_time"], TIME_FORMAT)
            latest = datetime.strptime(source["latest_arrival_time"], TIME_FORMAT)
            window_minutes = round((latest - earliest).total_seconds() / 60)
            row.update(
                {
                    "earliest": earliest.strftime(TIME_FORMAT),
                    "latest": latest.strftime(TIME_FORMAT),
                    "task_type": source["task_type"],
                    "priority": config.task_priority[source["task_type"]],
                    "window_minutes": window_minutes,
                    "slack_minutes": window_minutes - lower_minutes,
                    "potentially_tight": int(window_minutes - lower_minutes < 30),
                }
            )
        rows.append(row)
    return rows


def build_od(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["origin"]), str(row["destination"]), str(row["direction"]))].append(str(row["person_id"]))
    return [
        {
            "origin": origin,
            "destination": destination,
            "direction": direction,
            "demand_count": len(person_ids),
            "person_ids": "|".join(person_ids),
        }
        for (origin, destination, direction), person_ids in sorted(grouped.items())
    ]


def build_leg_features(
    nodes: list[str], matrix: dict[str, dict[str, float]], config: ProblemConfig
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for origin in nodes:
        for destination in nodes:
            if origin == destination:
                continue
            for aircraft_type, aircraft in config.aircraft_types.items():
                distance = matrix[origin][destination]
                fuel = fuel_for_leg(distance, aircraft)
                rows.append(
                    {
                        "from_node": origin,
                        "to_node": destination,
                        "aircraft_type": aircraft_type,
                        "distance_km": distance,
                        "flight_minutes": flight_minutes(distance, aircraft.speed_kmh),
                        "fuel_consumption_kg": round(fuel, 6),
                        "full_tank_leg_feasible": int(
                            aircraft.tank_capacity_kg - fuel + EPSILON >= aircraft.reserve_kg
                        ),
                        "from_can_refuel": int(origin in config.refuel_facilities),
                        "to_can_refuel": int(destination in config.refuel_facilities),
                    }
                )
    return rows


def _fuel_network_analysis(
    nodes: list[str], matrix: dict[str, dict[str, float]], config: ProblemConfig
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for aircraft_type, aircraft in config.aircraft_types.items():
        for facility in config.facilities:
            nearest_refuel = min((matrix[facility][node], node) for node in config.refuel_facilities)
            for airport in config.airports:
                outbound = matrix[airport][facility]
                direct_leg = outbound <= aircraft.full_tank_usable_distance_km + EPSILON
                direct_round_trip = 2 * outbound <= aircraft.full_tank_usable_distance_km + EPSILON
                rows.append(
                    {
                        "aircraft_type": aircraft_type,
                        "airport": airport,
                        "facility": facility,
                        "airport_distance_km": outbound,
                        "full_tank_direct_leg_feasible": int(direct_leg),
                        "full_tank_direct_round_trip_feasible": int(direct_round_trip),
                        "nearest_refuel_facility": nearest_refuel[1],
                        "nearest_refuel_distance_km": nearest_refuel[0],
                        "likely_refuel_dependent": int(not direct_round_trip),
                    }
                )
    return rows


def prepare_data(root: Path = ROOT) -> dict[str, object]:
    config = load_config()
    paths = default_paths(root)
    manifest = protect_raw_inputs(paths)
    people_rows = {name: read_csv(paths.raw_dir / name) for name in INPUT_NAMES[1:]}
    demand_nodes = {
        node
        for rows in people_rows.values()
        for row in rows
        for node in (row["origin_id"], row["destination_id"])
        if node != "LAND"
    }
    distance_report, nodes, matrix = validate_distance_data(
        paths.raw_dir / "distances.csv", config, demand_nodes
    )
    people_reports = {
        "q1": validate_people_file(paths.raw_dir / "peopleQ1.csv", "q1", config),
        "q2": validate_people_file(paths.raw_dir / "peopleQ2.csv", "q2", config),
        "q3": validate_people_file(paths.raw_dir / "peopleQ3.csv", "q3", config),
    }
    cross = cross_file_checks(paths.raw_dir)
    all_passed = distance_report["passed"] and all(report["passed"] for report in people_reports.values()) and all(cross.values())
    if not all_passed:
        raise ValueError("Official input validation failed; see generated data quality details")

    canonical = {
        "q1": build_demands(people_rows["peopleQ1.csv"], "q1", matrix, config),
        "q2": build_demands(people_rows["peopleQ2.csv"], "q2", matrix, config),
        "q3": build_demands(people_rows["peopleQ3.csv"], "q3", matrix, config),
    }
    nodes_rows = build_nodes(config)
    aircraft_rows = build_aircraft_types(config)
    od_q1 = build_od(canonical["q1"])
    od_q2 = build_od(canonical["q2"])
    leg_features = build_leg_features(nodes, matrix, config)
    fuel_network = _fuel_network_analysis(nodes, matrix, config)
    closed_route_reachability = _closed_route_reachability(matrix, config)
    refuel_hubs = _refuel_hub_summary(matrix, config)

    processed = paths.processed_dir
    write_csv(processed / "nodes.csv", list(nodes_rows[0]), nodes_rows)
    write_csv(processed / "aircraft_types.csv", list(aircraft_rows[0]), aircraft_rows)
    for question in ("q1", "q2", "q3"):
        write_csv(processed / f"demands_{question}.csv", list(canonical[question][0]), canonical[question])
    write_csv(processed / "od_q1.csv", list(od_q1[0]), od_q1)
    write_csv(processed / "od_q2.csv", list(od_q2[0]), od_q2)
    write_csv(processed / "features" / "leg_features.csv", list(leg_features[0]), leg_features)
    write_csv(processed / "features" / "fuel_network.csv", list(fuel_network[0]), fuel_network)
    write_csv(
        processed / "features" / "closed_route_reachability.csv",
        list(closed_route_reachability[0]),
        closed_route_reachability,
    )
    write_csv(processed / "features" / "refuel_hub_summary.csv", list(refuel_hubs[0]), refuel_hubs)
    quality = {
        "raw_manifest": manifest,
        "distance": distance_report,
        "people": people_reports,
        "cross_file": cross,
        "all_passed": all_passed,
    }
    write_json(processed / "data_quality.json", quality)
    summary = {
        "nodes": len(nodes_rows),
        "aircraft_types": len(aircraft_rows),
        "demands_q1": len(canonical["q1"]),
        "demands_q2": len(canonical["q2"]),
        "demands_q3": len(canonical["q3"]),
        "od_q1": len(od_q1),
        "od_q2": len(od_q2),
        "leg_features": len(leg_features),
        "fuel_network_rows": len(fuel_network),
        "closed_route_reachability_rows": len(closed_route_reachability),
        "refuel_hub_rows": len(refuel_hubs),
    }
    write_json(processed / "build_summary.json", summary)
    return {"paths": paths, "quality": quality, "summary": summary}
