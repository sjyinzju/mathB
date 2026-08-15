from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, linprog, milp
from scipy.sparse import coo_matrix, csr_matrix

from ..io_utils import sha256
from .alns import Q1ALNSConfig, _repair_neighborhood
from .cache import SolverCache
from .data import ProblemData
from .evaluator import evaluate_route
from .exporter import load_q1_solution
from .models import (
    PassengerAssignment,
    RouteEvaluation,
    RoutePlan,
    Solution,
    aggregate_evaluations,
)


@dataclass(frozen=True)
class RoutePoolSource:
    source_id: str
    directory: str
    algorithm: str
    seed: int | None
    objective: int
    flights: int
    routes_sha256: str
    assignments_sha256: str
    tier: str


@dataclass
class EliteRoute:
    route_id: str
    route: RoutePlan
    evaluation: RouteEvaluation
    service_order: tuple[str, ...]
    arrival_minutes: dict[str, int]
    capacity: int
    sources: list[str] = field(default_factory=list)
    source_algorithms: set[str] = field(default_factory=set)
    source_objectives: list[int] = field(default_factory=list)
    source_seeds: set[int] = field(default_factory=set)
    original_passenger_patterns: set[tuple[tuple[str, str, int], ...]] = field(
        default_factory=set
    )
    first_seen: int = 0
    best_solution_membership: bool = False
    tier: str = "EXPLORATION"

    @property
    def key(self) -> tuple[object, ...]:
        return route_identity(self.route)

    def metadata(self) -> dict[str, object]:
        service = set(self.service_order)
        return {
            "route_id": self.route_id,
            "sources": list(self.sources),
            "source_algorithms": sorted(self.source_algorithms),
            "source_seeds": sorted(self.source_seeds),
            "best_source_objective": min(self.source_objectives),
            "base_airport": self.route.base_airport,
            "aircraft_type": self.route.aircraft_type,
            "ordered_service_nodes": list(self.service_order),
            "physical_stops": [
                {
                    "facility_id": stop.facility_id,
                    "refuel": stop.refuel,
                    "is_service": stop.is_service,
                }
                for stop in self.route.stops
            ],
            "duration": self.evaluation.total_aircraft_time_minutes,
            "fuel": self.evaluation.total_fuel_consumption_kg,
            "capacity": self.capacity,
            "original_passenger_patterns": [list(pattern) for pattern in sorted(self.original_passenger_patterns)],
            "technical_stops": [
                stop.facility_id
                for stop in self.route.stops[1:-1]
                if stop.refuel or stop.facility_id not in service
            ],
            "route_legality": self.evaluation.feasible,
            "first_seen": self.first_seen,
            "best_solution_membership": self.best_solution_membership,
            "tier": self.tier,
        }


@dataclass(frozen=True)
class EliteRoutePool:
    routes: tuple[EliteRoute, ...]
    sources: tuple[RoutePoolSource, ...]
    duplicate_routes: int
    duplicate_solutions: int
    skipped_sources: int

    def metadata(self) -> dict[str, object]:
        return {
            "unique_routes": len(self.routes),
            "sources": [asdict(source) for source in self.sources],
            "duplicate_routes": self.duplicate_routes,
            "duplicate_solutions": self.duplicate_solutions,
            "skipped_sources": self.skipped_sources,
            "tier_counts": {
                tier: sum(route.tier == tier for route in self.routes)
                for tier in ("CORE", "ELITE", "EXPLORATION")
            },
            "routes": [route.metadata() for route in self.routes],
        }


@dataclass(frozen=True)
class Q1MasterConfig:
    primary_time_limit_seconds: float = 120.0
    secondary_time_limit_seconds: float = 60.0
    mip_relative_gap: float = 0.0
    maximum_flights: int = 160
    primary_upper_bound_minutes: int | None = None
    maximum_total_flights: int | None = None


@dataclass(frozen=True)
class Q1RestrictedLPResult:
    objective: float
    status: int
    success: bool
    elapsed_seconds: float
    demand_duals: dict[str, float]
    selected_fractional_routes: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class Q1MasterResult:
    solution: Solution
    selected_multiplicity: dict[str, int]
    allocation: dict[str, int]
    primary_objective: int
    passenger_objective: int
    flights_objective: int
    fuel_objective_kg: float
    primary_status: int
    primary_proven_optimal: bool
    primary_dual_bound: float | None
    primary_mip_gap: float | None
    stage_statuses: dict[str, int]
    elapsed_seconds: float
    variable_count: int
    constraint_count: int
    compatible_allocations: int


@dataclass(frozen=True)
class Q1TargetedRepairResult:
    solution: Solution | None
    reason: str
    route_indices: tuple[int, ...]
    routes_before: int
    routes_after: int | None
    passengers_affected: int
    aircraft_time_before: int
    aircraft_time_after: int | None
    elapsed_seconds: float
    diagnostics: dict[str, object]


@dataclass(frozen=True)
class _MasterArrays:
    routes: tuple[EliteRoute, ...]
    group_keys: tuple[tuple[str, str], ...]
    x_column: dict[tuple[int, int], int]
    equality: csr_matrix
    equality_rhs: np.ndarray
    upper: csr_matrix
    upper_rhs: np.ndarray
    bounds: Bounds
    primary: np.ndarray
    passenger: np.ndarray
    flights: np.ndarray
    fuel_deci_kg: np.ndarray


@dataclass(frozen=True)
class _PatternColumn:
    column_id: str
    elite_route: EliteRoute
    pattern: tuple[tuple[str, str, int], ...]
    passenger_time_minutes: int


@dataclass(frozen=True)
class _PatternArrays:
    columns: tuple[_PatternColumn, ...]
    group_keys: tuple[tuple[str, str], ...]
    equality: csr_matrix
    equality_rhs: np.ndarray
    bounds: Bounds
    primary: np.ndarray
    passenger: np.ndarray
    flights: np.ndarray
    fuel_deci_kg: np.ndarray


def route_identity(route: RoutePlan) -> tuple[object, ...]:
    """Identity preserves order, physical/refuel semantics and service compatibility."""
    service_order = tuple(dict.fromkeys(route.service_facilities))
    if not service_order:
        service_order = tuple(
            dict.fromkeys(item.destination_id for item in route.assignments)
        )
    return (
        route.base_airport,
        route.aircraft_type,
        tuple(
            (stop.facility_id, bool(stop.refuel), bool(stop.is_service))
            for stop in route.stops
        ),
        service_order,
    )


def _route_id(key: tuple[object, ...]) -> str:
    payload = json.dumps(key, ensure_ascii=True, separators=(",", ":"))
    return "Q1R-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _source_tier(objective: int) -> str:
    if objective <= 14772:
        return "CORE"
    if objective <= 15118:
        return "ELITE"
    return "EXPLORATION"


def _algorithm_and_seed(directory: Path) -> tuple[str, int | None]:
    for name in ("run_summary.json", "run_config.json", "winning_config.json"):
        path = directory / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        algorithm = str(
            payload.get("method")
            or payload.get("experiment")
            or payload.get("run_id")
            or directory.name
        )
        seed = payload.get("seed")
        if seed is None and isinstance(payload.get("config"), dict):
            seed = payload["config"].get("seed")
        return algorithm, int(seed) if seed is not None else None
    return directory.name, None


def discover_elite_sources(
    output_root: Path | str,
    *,
    maximum_objective: int = 15371,
    exact_objective: int | None = None,
) -> tuple[list[tuple[Path, dict[str, object]]], int, int]:
    output_root = Path(output_root)
    discovered: list[tuple[Path, dict[str, object]]] = []
    seen_solution_hashes: set[tuple[str, str]] = set()
    duplicates = 0
    skipped = 0
    for validator_path in sorted(output_root.rglob("validator.json")):
        directory = validator_path.parent
        routes_path = directory / "q1-routes.csv"
        assignments_path = directory / "q1-assignments.csv"
        if not routes_path.exists() or not assignments_path.exists():
            skipped += 1
            continue
        try:
            validator = json.loads(validator_path.read_text(encoding="utf-8"))
            metrics = validator.get("metrics") or {}
            objective = int(metrics["total_aircraft_time_minutes"])
            flights = int(metrics["total_flights"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            skipped += 1
            continue
        if not validator.get("valid") or objective > maximum_objective:
            skipped += 1
            continue
        if exact_objective is not None and objective != exact_objective:
            skipped += 1
            continue
        solution_hash = (sha256(routes_path), sha256(assignments_path))
        if solution_hash in seen_solution_hashes:
            duplicates += 1
            continue
        seen_solution_hashes.add(solution_hash)
        discovered.append(
            (
                directory,
                {
                    "objective": objective,
                    "flights": flights,
                    "routes_sha256": solution_hash[0],
                    "assignments_sha256": solution_hash[1],
                },
            )
        )
    discovered.sort(key=lambda item: (int(item[1]["objective"]), str(item[0])))
    return discovered, duplicates, skipped


def collect_elite_route_pool(
    data: ProblemData,
    output_root: Path | str,
    *,
    maximum_objective: int = 15371,
    exact_objective: int | None = None,
    source_directories: Sequence[Path | str] | None = None,
) -> EliteRoutePool:
    if source_directories is None:
        discovered, duplicate_solutions, skipped = discover_elite_sources(
            output_root,
            maximum_objective=maximum_objective,
            exact_objective=exact_objective,
        )
    else:
        discovered = []
        duplicate_solutions = 0
        skipped = 0
        seen_hashes: set[tuple[str, str]] = set()
        for raw_directory in source_directories:
            directory = Path(raw_directory)
            routes_path = directory / "q1-routes.csv"
            assignments_path = directory / "q1-assignments.csv"
            validator_path = directory / "validator.json"
            if not validator_path.exists():
                validator_path = directory / "q1-validator.json"
            if not routes_path.exists() or not assignments_path.exists() or not validator_path.exists():
                raise FileNotFoundError(f"Incomplete explicit route-pool source: {directory}")
            validator = json.loads(validator_path.read_text(encoding="utf-8"))
            metrics = validator.get("metrics") or {}
            if not validator.get("valid"):
                raise ValueError(f"Explicit route-pool source is not VALID: {directory}")
            hashes = (sha256(routes_path), sha256(assignments_path))
            if hashes in seen_hashes:
                duplicate_solutions += 1
                continue
            seen_hashes.add(hashes)
            discovered.append(
                (
                    directory,
                    {
                        "objective": int(metrics["total_aircraft_time_minutes"]),
                        "flights": int(metrics["total_flights"]),
                        "routes_sha256": hashes[0],
                        "assignments_sha256": hashes[1],
                    },
                )
            )
        discovered.sort(key=lambda item: (int(item[1]["objective"]), str(item[0])))
    entries: dict[tuple[object, ...], EliteRoute] = {}
    sources: list[RoutePoolSource] = []
    duplicate_routes = 0
    first_seen = 0
    for source_index, (directory, raw) in enumerate(discovered):
        solution = load_q1_solution(
            directory / "q1-routes.csv",
            directory / "q1-assignments.csv",
            data,
            method="q1_elite_pool_source",
        )
        objective = int(raw["objective"])
        algorithm, seed = _algorithm_and_seed(directory)
        source_id = f"S{source_index:03d}-{raw['routes_sha256'][:10]}"
        tier = _source_tier(objective)
        sources.append(
            RoutePoolSource(
                source_id=source_id,
                directory=str(directory),
                algorithm=algorithm,
                seed=seed,
                objective=objective,
                flights=int(raw["flights"]),
                routes_sha256=str(raw["routes_sha256"]),
                assignments_sha256=str(raw["assignments_sha256"]),
                tier=tier,
            )
        )
        for route in solution.routes:
            first_seen += 1
            evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
            if not evaluation.feasible:
                raise ValueError(f"Infeasible elite route in {directory}: {evaluation.issues}")
            service_order = tuple(dict.fromkeys(route.service_facilities))
            if not service_order:
                service_order = tuple(
                    dict.fromkeys(item.destination_id for item in route.assignments)
                )
            empty_route = RoutePlan(
                base_airport=route.base_airport,
                aircraft_type=route.aircraft_type,
                stops=route.stops,
                assignments=(),
                service_facilities=service_order,
            )
            empty_evaluation = evaluate_route(
                empty_route, matrix=data.matrix, config=data.config
            )
            arrival_minutes: dict[str, int] = {}
            for destination in service_order:
                deliveries = [
                    item.delivery_stop_order
                    for item in route.assignments
                    if item.destination_id == destination
                ]
                if not deliveries:
                    raise ValueError(f"Service destination {destination} has no assignment")
                delivery = min(deliveries)
                arrival_minutes[destination] = next(
                    sum(leg.flight_minutes for leg in evaluation.legs[:delivery])
                    + sum(
                        data.config.stop_with_refuel_minutes
                        if route.stops[index].refuel
                        else data.config.stop_without_refuel_minutes
                        for index in range(1, delivery)
                    )
                    for _ in (0,)
                )
            key = route_identity(empty_route)
            pattern_counts: dict[tuple[str, str], int] = defaultdict(int)
            for assignment in route.assignments:
                pattern_counts[(assignment.origin_id, assignment.destination_id)] += 1
            passenger_pattern = tuple(
                (origin, destination, count)
                for (origin, destination), count in sorted(pattern_counts.items())
            )
            entry = entries.get(key)
            if entry is None:
                entry = EliteRoute(
                    route_id=_route_id(key),
                    route=empty_route,
                    evaluation=empty_evaluation,
                    service_order=service_order,
                    arrival_minutes=arrival_minutes,
                    capacity=data.config.aircraft_types[route.aircraft_type].seats,
                    first_seen=first_seen,
                )
                entries[key] = entry
            else:
                duplicate_routes += 1
            entry.sources.append(source_id)
            entry.source_algorithms.add(algorithm)
            entry.source_objectives.append(objective)
            if seed is not None:
                entry.source_seeds.add(seed)
            entry.original_passenger_patterns.add(passenger_pattern)
            entry.best_solution_membership |= objective == 14770
            if tier == "CORE" or (tier == "ELITE" and entry.tier == "EXPLORATION"):
                entry.tier = tier
    ordered = tuple(
        sorted(
            entries.values(),
            key=lambda route: (
                route.evaluation.total_aircraft_time_minutes,
                route.route.base_airport,
                route.route.aircraft_type,
                route.service_order,
                route.route_id,
            ),
        )
    )
    return EliteRoutePool(
        routes=ordered,
        sources=tuple(sources),
        duplicate_routes=duplicate_routes,
        duplicate_solutions=duplicate_solutions,
        skipped_sources=skipped,
    )


def _build_master_arrays(
    data: ProblemData,
    pool: EliteRoutePool,
    config: Q1MasterConfig,
) -> _MasterArrays:
    routes = pool.routes
    group_keys = tuple(sorted(data.q1_pools))
    y_count = len(routes)
    x_pairs: list[tuple[int, int]] = []
    for group_id, (origin, destination) in enumerate(group_keys):
        for route_id, route in enumerate(routes):
            if destination in route.arrival_minutes and (
                origin == "LAND" or origin == route.route.base_airport
            ):
                x_pairs.append((group_id, route_id))
    x_column = {
        pair: y_count + index for index, pair in enumerate(sorted(x_pairs))
    }
    if any(
        not any(pair[0] == group_id for pair in x_column)
        for group_id in range(len(group_keys))
    ):
        missing = [
            group_keys[group_id]
            for group_id in range(len(group_keys))
            if not any(pair[0] == group_id for pair in x_column)
        ]
        raise ValueError(f"Route pool cannot cover demand groups: {missing}")

    variable_count = y_count + len(x_column)
    eq_rows: list[int] = []
    eq_columns: list[int] = []
    eq_values: list[float] = []
    equality_rhs: list[float] = []
    for group_id, key in enumerate(group_keys):
        for (other_group, route_id), column in x_column.items():
            if other_group == group_id:
                eq_rows.append(group_id)
                eq_columns.append(column)
                eq_values.append(1.0)
        equality_rhs.append(float(data.q1_pools[key].quantity))
    equality = coo_matrix(
        (np.asarray(eq_values), (np.asarray(eq_rows), np.asarray(eq_columns))),
        shape=(len(group_keys), variable_count),
    ).tocsr()

    ub_rows: list[int] = []
    ub_columns: list[int] = []
    ub_values: list[float] = []
    upper_rhs: list[float] = []

    def add_upper(coefficients: Iterable[tuple[int, float]], rhs: float) -> None:
        row = len(upper_rhs)
        for column, value in coefficients:
            ub_rows.append(row)
            ub_columns.append(column)
            ub_values.append(value)
        upper_rhs.append(rhs)

    for route_id, route in enumerate(routes):
        capacity = [(route_id, -float(route.capacity))]
        capacity.extend(
            (column, 1.0)
            for (group_id, other_route), column in x_column.items()
            if other_route == route_id
        )
        add_upper(capacity, 0.0)
        for destination in route.service_order:
            coverage = [(route_id, 1.0)]
            coverage.extend(
                (column, -1.0)
                for (group_id, other_route), column in x_column.items()
                if other_route == route_id
                and group_keys[group_id][1] == destination
            )
            add_upper(coverage, 0.0)
    upper = coo_matrix(
        (np.asarray(ub_values), (np.asarray(ub_rows), np.asarray(ub_columns))),
        shape=(len(upper_rhs), variable_count),
    ).tocsr()

    lower_bounds = np.zeros(variable_count)
    upper_bounds = np.empty(variable_count)
    upper_bounds[:y_count] = config.maximum_flights
    for (group_id, _), column in x_column.items():
        upper_bounds[column] = data.q1_pools[group_keys[group_id]].quantity

    primary = np.zeros(variable_count)
    passenger = np.zeros(variable_count)
    flights = np.zeros(variable_count)
    fuel = np.zeros(variable_count)
    for route_id, route in enumerate(routes):
        primary[route_id] = route.evaluation.total_aircraft_time_minutes
        flights[route_id] = 1.0
        fuel[route_id] = round(route.evaluation.total_fuel_consumption_kg * 10.0)
    for (group_id, route_id), column in x_column.items():
        passenger[column] = routes[route_id].arrival_minutes[group_keys[group_id][1]]
    return _MasterArrays(
        routes=routes,
        group_keys=group_keys,
        x_column=x_column,
        equality=equality,
        equality_rhs=np.asarray(equality_rhs),
        upper=upper,
        upper_rhs=np.asarray(upper_rhs),
        bounds=Bounds(lower_bounds, upper_bounds),
        primary=primary,
        passenger=passenger,
        flights=flights,
        fuel_deci_kg=fuel,
    )


def _build_pattern_arrays(
    data: ProblemData,
    pool: EliteRoutePool,
    config: Q1MasterConfig,
) -> _PatternArrays:
    group_keys = tuple(sorted(data.q1_pools))
    group_index = {key: index for index, key in enumerate(group_keys)}
    columns: list[_PatternColumn] = []
    seen: set[tuple[str, tuple[tuple[str, str, int], ...]]] = set()
    for elite in pool.routes:
        for pattern in sorted(elite.original_passenger_patterns):
            key = (elite.route_id, pattern)
            if key in seen:
                continue
            seen.add(key)
            if not pattern or sum(count for _, _, count in pattern) > elite.capacity:
                continue
            if any((origin, destination) not in group_index for origin, destination, _ in pattern):
                continue
            passenger_time = sum(
                count * elite.arrival_minutes[destination]
                for _, destination, count in pattern
            )
            payload = json.dumps(key, ensure_ascii=True, separators=(",", ":"))
            columns.append(
                _PatternColumn(
                    column_id="Q1C-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
                    elite_route=elite,
                    pattern=pattern,
                    passenger_time_minutes=passenger_time,
                )
            )
    columns.sort(
        key=lambda column: (
            column.elite_route.evaluation.total_aircraft_time_minutes,
            column.elite_route.route_id,
            column.pattern,
        )
    )
    if not columns:
        raise ValueError("Route pool contains no allocated route-pattern columns")

    rows: list[int] = []
    matrix_columns: list[int] = []
    values: list[float] = []
    for column_id, column in enumerate(columns):
        for origin, destination, count in column.pattern:
            rows.append(group_index[(origin, destination)])
            matrix_columns.append(column_id)
            values.append(float(count))
    equality = coo_matrix(
        (np.asarray(values), (np.asarray(rows), np.asarray(matrix_columns))),
        shape=(len(group_keys), len(columns)),
    ).tocsr()
    equality_rhs = np.asarray(
        [float(data.q1_pools[key].quantity) for key in group_keys]
    )
    missing = [
        group_keys[index]
        for index in range(len(group_keys))
        if equality.getrow(index).nnz == 0
    ]
    if missing:
        raise ValueError(f"Pattern route pool cannot cover demand groups: {missing}")

    upper = np.empty(len(columns))
    for column_id, column in enumerate(columns):
        maximum = config.maximum_flights
        for origin, destination, count in column.pattern:
            maximum = min(
                maximum,
                data.q1_pools[(origin, destination)].quantity // count,
            )
        upper[column_id] = maximum
    primary = np.asarray(
        [column.elite_route.evaluation.total_aircraft_time_minutes for column in columns],
        dtype=float,
    )
    passenger = np.asarray(
        [column.passenger_time_minutes for column in columns], dtype=float
    )
    flights = np.ones(len(columns))
    fuel = np.asarray(
        [
            round(column.elite_route.evaluation.total_fuel_consumption_kg * 10.0)
            for column in columns
        ],
        dtype=float,
    )
    return _PatternArrays(
        columns=tuple(columns),
        group_keys=group_keys,
        equality=equality,
        equality_rhs=equality_rhs,
        bounds=Bounds(np.zeros(len(columns)), upper),
        primary=primary,
        passenger=passenger,
        flights=flights,
        fuel_deci_kg=fuel,
    )


def _materialize_pattern_master(
    data: ProblemData,
    arrays: _PatternArrays,
    selected: np.ndarray,
) -> tuple[Solution, dict[str, int], dict[str, int]]:
    multiplicities = np.rint(selected).astype(int)
    remaining = {
        key: list(data.q1_pools[key].person_ids) for key in arrays.group_keys
    }
    routes: list[RoutePlan] = []
    evaluations: list[RouteEvaluation] = []
    selected_multiplicity: dict[str, int] = {}
    allocation: dict[str, int] = {}
    for column_id, multiplicity in enumerate(multiplicities):
        if multiplicity <= 0:
            continue
        column = arrays.columns[column_id]
        elite = column.elite_route
        selected_multiplicity[column.column_id] = int(multiplicity)
        locations = tuple(stop.facility_id for stop in elite.route.stops)
        for copy in range(int(multiplicity)):
            assignments: list[PassengerAssignment] = []
            for origin, destination, count in column.pattern:
                key = (origin, destination)
                people = remaining[key][:count]
                del remaining[key][:count]
                if len(people) != count:
                    raise RuntimeError("Pattern master allocation exceeds demand")
                allocation[f"{origin}->{destination}@{column.column_id}"] = (
                    allocation.get(f"{origin}->{destination}@{column.column_id}", 0)
                    + count
                )
                delivery = locations.index(destination, 1)
                assignments.extend(
                    PassengerAssignment(person, origin, destination, 0, delivery)
                    for person in people
                )
            route = RoutePlan(
                base_airport=elite.route.base_airport,
                aircraft_type=elite.route.aircraft_type,
                stops=elite.route.stops,
                assignments=tuple(sorted(assignments, key=lambda item: item.person_id)),
                service_facilities=elite.service_order,
            )
            evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
            if not evaluation.feasible:
                raise RuntimeError(f"Pattern master route invalid: {evaluation.issues}")
            routes.append(route)
            evaluations.append(evaluation)
    if any(remaining.values()):
        raise RuntimeError("Pattern master did not cover all passengers")
    solution = Solution(
        routes=tuple(routes),
        metrics=aggregate_evaluations(evaluations, data.q1_passenger_count),
        method="q1_exact_allocated_route_pattern_master",
        diagnostics={
            "route_pool": {
                "unique_physical_routes": len({column.elite_route.route_id for column in arrays.columns}),
                "allocated_pattern_columns": len(arrays.columns),
                "selected_columns": len(selected_multiplicity),
            }
        },
    )
    return solution, selected_multiplicity, allocation


def solve_restricted_lp(
    data: ProblemData,
    pool: EliteRoutePool,
    config: Q1MasterConfig | None = None,
) -> Q1RestrictedLPResult:
    config = config or Q1MasterConfig()
    arrays = _build_pattern_arrays(data, pool, config)
    started = time.perf_counter()
    result = linprog(
        arrays.primary,
        A_eq=arrays.equality,
        b_eq=arrays.equality_rhs,
        bounds=list(zip(arrays.bounds.lb, arrays.bounds.ub)),
        method="highs",
    )
    if result.x is None:
        raise RuntimeError(f"Restricted LP failed: status={result.status}, {result.message}")
    demand_duals = {
        f"{origin}->{destination}": float(result.eqlin.marginals[index])
        for index, (origin, destination) in enumerate(arrays.group_keys)
    }
    fractional = []
    for column_id, value in enumerate(result.x):
        if value > 1.0e-8 and abs(value - round(value)) > 1.0e-7:
            column = arrays.columns[column_id]
            fractional.append(
                {
                    "column_id": column.column_id,
                    "route_id": column.elite_route.route_id,
                    "multiplicity": float(value),
                    "duration": column.elite_route.evaluation.total_aircraft_time_minutes,
                    "service_order": list(column.elite_route.service_order),
                }
            )
    fractional.sort(key=lambda row: -float(row["multiplicity"]))
    return Q1RestrictedLPResult(
        objective=float(result.fun),
        status=int(result.status),
        success=bool(result.success),
        elapsed_seconds=round(time.perf_counter() - started, 6),
        demand_duals=demand_duals,
        selected_fractional_routes=tuple(fractional),
    )


def _materialize_master(
    data: ProblemData,
    arrays: _MasterArrays,
    selected: np.ndarray,
) -> tuple[Solution, dict[str, int], dict[str, int]]:
    y_values = np.rint(selected[: len(arrays.routes)]).astype(int)
    x_values = {
        pair: int(round(float(selected[column])))
        for pair, column in arrays.x_column.items()
        if selected[column] > 0.5
    }
    remaining = {
        key: list(data.q1_pools[key].person_ids) for key in arrays.group_keys
    }
    materialized_routes: list[RoutePlan] = []
    evaluations: list[RouteEvaluation] = []
    selected_multiplicity: dict[str, int] = {}
    allocation: dict[str, int] = {}
    for route_id, multiplicity in enumerate(y_values):
        if multiplicity <= 0:
            continue
        elite = arrays.routes[route_id]
        selected_multiplicity[elite.route_id] = int(multiplicity)
        by_destination: dict[str, list[PassengerAssignment]] = defaultdict(list)
        for group_id, key in enumerate(arrays.group_keys):
            count = x_values.get((group_id, route_id), 0)
            if not count:
                continue
            people = remaining[key][:count]
            del remaining[key][:count]
            if len(people) != count:
                raise RuntimeError("Master allocation exceeds remaining demand")
            allocation[f"{key[0]}->{key[1]}@{elite.route_id}"] = count
            by_destination[key[1]].extend(
                PassengerAssignment(person, key[0], key[1], 0, 0)
                for person in people
            )

        bins: list[list[PassengerAssignment]] = [
            [] for _ in range(int(multiplicity))
        ]
        for destination in elite.service_order:
            available = by_destination[destination]
            if len(available) < multiplicity:
                raise RuntimeError("Master service-use constraint was not materializable")
            for bin_index in range(int(multiplicity)):
                bins[bin_index].append(available.pop())
        extras = [
            person
            for destination in elite.service_order
            for person in by_destination[destination]
        ]
        extras.sort(key=lambda person: (person.destination_id, person.person_id))
        for person in extras:
            target = min(range(len(bins)), key=lambda index: (len(bins[index]), index))
            if len(bins[target]) >= elite.capacity:
                raise RuntimeError("Master capacity allocation was not materializable")
            bins[target].append(person)
        locations = tuple(stop.facility_id for stop in elite.route.stops)
        for people in bins:
            if not people or len(people) > elite.capacity:
                raise RuntimeError("Master created an empty or overloaded flight")
            assignments = tuple(
                PassengerAssignment(
                    person_id=person.person_id,
                    origin_id=person.origin_id,
                    destination_id=person.destination_id,
                    pickup_stop_order=0,
                    delivery_stop_order=locations.index(person.destination_id, 1),
                )
                for person in sorted(people, key=lambda item: item.person_id)
            )
            route = RoutePlan(
                base_airport=elite.route.base_airport,
                aircraft_type=elite.route.aircraft_type,
                stops=elite.route.stops,
                assignments=assignments,
                service_facilities=elite.service_order,
            )
            evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
            if not evaluation.feasible:
                raise RuntimeError(f"Materialized master route invalid: {evaluation.issues}")
            materialized_routes.append(route)
            evaluations.append(evaluation)
    if any(remaining.values()):
        raise RuntimeError("Master materialization did not cover all passengers")
    solution = Solution(
        routes=tuple(materialized_routes),
        metrics=aggregate_evaluations(evaluations, data.q1_passenger_count),
        method="q1_exact_restricted_route_pool_master",
        diagnostics={
            "route_pool": {
                "unique_routes": len(arrays.routes),
                "selected_route_patterns": len(selected_multiplicity),
            }
        },
    )
    return solution, selected_multiplicity, allocation


def solve_route_pool_master(
    data: ProblemData,
    pool: EliteRoutePool,
    config: Q1MasterConfig | None = None,
) -> Q1MasterResult:
    config = config or Q1MasterConfig()
    arrays = _build_pattern_arrays(data, pool, config)
    started = time.perf_counter()
    constraints: list[LinearConstraint] = [
        LinearConstraint(arrays.equality, arrays.equality_rhs, arrays.equality_rhs),
    ]
    if config.primary_upper_bound_minutes is not None:
        constraints.append(
            LinearConstraint(
                coo_matrix(arrays.primary.reshape(1, -1)).tocsr(),
                -np.inf,
                float(config.primary_upper_bound_minutes),
            )
        )
    if config.maximum_total_flights is not None:
        constraints.append(
            LinearConstraint(
                coo_matrix(arrays.flights.reshape(1, -1)).tocsr(),
                -np.inf,
                float(config.maximum_total_flights),
            )
        )
    integrality = np.ones(len(arrays.primary), dtype=np.uint8)

    def solve_stage(objective: np.ndarray, seconds: float):
        return milp(
            c=objective,
            integrality=integrality,
            bounds=arrays.bounds,
            constraints=constraints,
            options={
                "time_limit": seconds,
                "mip_rel_gap": config.mip_relative_gap,
                "presolve": True,
            },
        )

    primary = solve_stage(arrays.primary, config.primary_time_limit_seconds)
    if primary.x is None:
        raise RuntimeError(f"Restricted integer master failed: status={primary.status}")
    selected = primary.x
    stage_statuses = {"primary": int(primary.status)}
    objectives = (
        ("passenger", arrays.passenger),
        ("flights", arrays.flights),
        ("fuel_deci_kg", arrays.fuel_deci_kg),
    )
    primary_value = int(round(float(arrays.primary @ selected)))
    constraints.append(
        LinearConstraint(
            coo_matrix(arrays.primary.reshape(1, -1)).tocsr(),
            float(primary_value),
            float(primary_value),
        )
    )
    for name, objective in objectives:
        result = solve_stage(objective, config.secondary_time_limit_seconds)
        stage_statuses[name] = int(result.status)
        if result.x is None:
            continue
        selected = result.x
        value = int(round(float(objective @ selected)))
        constraints.append(
            LinearConstraint(
                coo_matrix(objective.reshape(1, -1)).tocsr(),
                float(value),
                float(value),
            )
        )
    solution, multiplicity, allocation = _materialize_pattern_master(data, arrays, selected)
    return Q1MasterResult(
        solution=solution,
        selected_multiplicity=multiplicity,
        allocation=allocation,
        primary_objective=solution.metrics.total_aircraft_time_minutes,
        passenger_objective=solution.metrics.total_passenger_travel_time_minutes,
        flights_objective=solution.metrics.total_flights,
        fuel_objective_kg=solution.metrics.total_fuel_consumption_kg,
        primary_status=int(primary.status),
        primary_proven_optimal=bool(int(primary.status) == 0),
        primary_dual_bound=(
            float(primary.mip_dual_bound)
            if getattr(primary, "mip_dual_bound", None) is not None
            else None
        ),
        primary_mip_gap=(
            float(primary.mip_gap)
            if getattr(primary, "mip_gap", None) is not None
            else None
        ),
        stage_statuses=stage_statuses,
        elapsed_seconds=round(time.perf_counter() - started, 6),
        variable_count=len(arrays.primary),
        constraint_count=arrays.equality.shape[0],
        compatible_allocations=arrays.equality.nnz,
    )


def route_elimination_audit(
    solution: Solution,
    data: ProblemData,
) -> tuple[dict[str, object], ...]:
    evaluations = [
        evaluate_route(route, matrix=data.matrix, config=data.config)
        for route in solution.routes
    ]
    rows: list[dict[str, object]] = []
    for index, (route, evaluation) in enumerate(zip(solution.routes, evaluations)):
        facilities = tuple(dict.fromkeys(route.service_facilities))
        if not facilities:
            facilities = tuple(
                dict.fromkeys(item.destination_id for item in route.assignments)
            )
        land = sum(item.origin_id == "LAND" for item in route.assignments)
        target_candidates: list[tuple[float, int]] = []
        for other_index, other in enumerate(solution.routes):
            if other_index == index:
                continue
            other_facilities = tuple(dict.fromkeys(other.service_facilities))
            if not other_facilities:
                other_facilities = tuple(
                    dict.fromkeys(item.destination_id for item in other.assignments)
                )
            distance = min(
                data.matrix[left][right]
                for left in facilities
                for right in other_facilities
            )
            base_penalty = 0.0 if route.base_airport == other.base_airport else 30.0
            target_candidates.append((distance + base_penalty, other_index))
        target_candidates.sort()
        passenger_count = len(route.assignments)
        land_flexibility = land / max(1, passenger_count)
        score = (
            evaluation.total_aircraft_time_minutes / max(1, passenger_count)
            + 120.0 * (1.0 - evaluation.seat_utilization)
            + 25.0 * land_flexibility
            + 10.0 / max(1.0, target_candidates[0][0] if target_candidates else 100.0)
        )
        rows.append(
            {
                "route_index": index,
                "aircraft_time": evaluation.total_aircraft_time_minutes,
                "passenger_count": passenger_count,
                "facility_set": list(facilities),
                "service_sequence": list(route.service_facilities),
                "aircraft_type": route.aircraft_type,
                "base_airport": route.base_airport,
                "capacity_utilization": evaluation.seat_utilization,
                "land_flexibility": land_flexibility,
                "neighbor_route_slack": sum(
                    max(
                        0,
                        data.config.aircraft_types[solution.routes[target].aircraft_type].seats
                        - solution.routes[target].passenger_count,
                    )
                    for _, target in target_candidates[:5]
                ),
                "facility_overlap": sum(
                    bool(set(facilities) & set(solution.routes[target].service_facilities))
                    for _, target in target_candidates[:5]
                ),
                "geometry_proximity": target_candidates[0][0]
                if target_candidates
                else None,
                "alternative_aircraft": sorted(data.config.aircraft_types),
                "possible_target_routes": [
                    target for _, target in target_candidates[:10]
                ],
                "elimination_potential": score,
            }
        )
    rows.sort(
        key=lambda row: (
            -float(row["elimination_potential"]),
            int(row["route_index"]),
        )
    )
    return tuple(rows)


def targeted_route_indices(
    solution: Solution,
    data: ProblemData,
    source_index: int,
    size: int,
    *,
    mode: str = "high_impact",
) -> tuple[int, ...]:
    if not (2 <= size <= len(solution.routes)):
        raise ValueError("targeted neighborhood size is out of range")
    source = solution.routes[source_index]
    source_facilities = tuple(dict.fromkeys(source.service_facilities))
    if not source_facilities:
        source_facilities = tuple(
            dict.fromkeys(item.destination_id for item in source.assignments)
        )
    ranked: list[tuple[tuple[float, ...], int]] = []
    for index, route in enumerate(solution.routes):
        if index == source_index:
            continue
        facilities = tuple(dict.fromkeys(route.service_facilities))
        if not facilities:
            facilities = tuple(
                dict.fromkeys(item.destination_id for item in route.assignments)
            )
        distance = min(
            data.matrix[left][right]
            for left in source_facilities
            for right in facilities
        )
        overlap = len(set(source_facilities) & set(facilities))
        base_penalty = 0.0 if source.base_airport == route.base_airport else 30.0
        if mode == "facility_block":
            key = (-float(overlap), distance, base_penalty, float(index))
        elif mode == "cross_exchange":
            key = (distance, -float(bool(set(source_facilities) ^ set(facilities))), base_penalty, float(index))
        else:
            key = (distance + base_penalty, -float(overlap), float(index))
        ranked.append((key, index))
    ranked.sort()
    return tuple(sorted([source_index, *(index for _, index in ranked[: size - 1])]))


def exact_targeted_repair(
    solution: Solution,
    data: ProblemData,
    route_indices: Sequence[int],
    *,
    reason: str,
    seed: int = 0,
    max_service_nodes: int = 3,
    max_long_service_orders: int = 120,
    repair_time_limit_seconds: float = 20.0,
    cache: SolverCache | None = None,
) -> Q1TargetedRepairResult:
    indices = tuple(sorted(set(int(index) for index in route_indices)))
    if len(indices) < 2:
        raise ValueError("Exact targeted repair needs at least two routes")
    evaluations = [
        evaluate_route(route, matrix=data.matrix, config=data.config)
        for route in solution.routes
    ]
    destroyed_routes = [solution.routes[index] for index in indices]
    destroyed_evaluations = [evaluations[index] for index in indices]
    passengers_affected = sum(route.passenger_count for route in destroyed_routes)
    removed_time = sum(
        evaluation.total_aircraft_time_minutes for evaluation in destroyed_evaluations
    )
    config = Q1ALNSConfig(
        iterations=1,
        time_limit_seconds=max(1.0, repair_time_limit_seconds + 1.0),
        min_destroy_routes=2,
        max_destroy_routes=max(2, len(indices)),
        max_service_nodes=max_service_nodes,
        max_long_service_orders=max_long_service_orders,
        repair_time_limit_seconds=repair_time_limit_seconds,
        seed=seed,
    )
    started = time.perf_counter()
    repaired = _repair_neighborhood(
        destroyed_routes,
        destroyed_evaluations,
        data,
        config,
        {},
        cache or SolverCache(data),
    )
    elapsed = time.perf_counter() - started
    pre_features = {
        "geometry": [list(route.service_facilities) for route in destroyed_routes],
        "route_time": [
            evaluation.total_aircraft_time_minutes
            for evaluation in destroyed_evaluations
        ],
        "route_slack": [
            data.config.aircraft_types[route.aircraft_type].seats - route.passenger_count
            for route in destroyed_routes
        ],
        "route_utilization": [
            evaluation.seat_utilization for evaluation in destroyed_evaluations
        ],
        "aircraft_type": [route.aircraft_type for route in destroyed_routes],
        "airport": [route.base_airport for route in destroyed_routes],
        "destroy_size": len(indices),
        "operator": reason,
        "current_objective": solution.metrics.total_aircraft_time_minutes,
    }
    if repaired is None:
        return Q1TargetedRepairResult(
            solution=None,
            reason=reason,
            route_indices=indices,
            routes_before=len(indices),
            routes_after=None,
            passengers_affected=passengers_affected,
            aircraft_time_before=removed_time,
            aircraft_time_after=None,
            elapsed_seconds=round(elapsed, 6),
            diagnostics={
                "pre_features": pre_features,
                "evaluated": True,
                "feasible": False,
                "label": "INVALID",
            },
        )
    kept = [
        route for index, route in enumerate(solution.routes) if index not in set(indices)
    ]
    kept_evaluations = [
        evaluation for index, evaluation in enumerate(evaluations) if index not in set(indices)
    ]
    candidate_routes = kept + repaired.routes
    candidate_evaluations = kept_evaluations + repaired.evaluations
    candidate = Solution(
        routes=tuple(candidate_routes),
        metrics=aggregate_evaluations(candidate_evaluations, data.q1_passenger_count),
        method=f"q1_targeted_exact_{reason}",
        diagnostics={
            **solution.diagnostics,
            "targeted_repair": {
                "reason": reason,
                "indices": list(indices),
                "variant_count": repaired.variant_count,
            },
        },
    )
    improved = candidate.metrics.comparison_key() < solution.metrics.comparison_key()
    primary_improved = (
        candidate.metrics.total_aircraft_time_minutes
        < solution.metrics.total_aircraft_time_minutes
    )
    return Q1TargetedRepairResult(
        solution=candidate,
        reason=reason,
        route_indices=indices,
        routes_before=len(indices),
        routes_after=len(repaired.routes),
        passengers_affected=passengers_affected,
        aircraft_time_before=removed_time,
        aircraft_time_after=sum(
            evaluation.total_aircraft_time_minutes
            for evaluation in repaired.evaluations
        ),
        elapsed_seconds=round(elapsed, 6),
        diagnostics={
            "pre_features": pre_features,
            "evaluated": True,
            "feasible": True,
            "repair_selected": improved,
            "repair_accepted": improved,
            "primary_improvement": primary_improved,
            "new_best": improved,
            "actual_delta_aircraft_time": (
                candidate.metrics.total_aircraft_time_minutes
                - solution.metrics.total_aircraft_time_minutes
            ),
            "evaluation_cost_seconds": round(elapsed, 6),
            "label": "POSITIVE" if improved else "TRUE_NEGATIVE",
            "variant_count": repaired.variant_count,
            "candidates_considered": repaired.candidates_considered,
            "candidates_selected": repaired.candidates_selected,
        },
    )
