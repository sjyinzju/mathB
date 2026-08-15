from __future__ import annotations

import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import permutations
from typing import Iterable

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix, vstack

from ..rules import flight_minutes, minimum_stop_minutes
from .data import ProblemData
from .evaluator import evaluate_route
from .models import (
    PassengerAssignment,
    RouteEvaluation,
    RoutePlan,
    Solution,
    SolverConfig,
    aggregate_evaluations,
)
from .cache import SolverCache
from .relatedness import (
    CONTEXT_COMPONENTS,
    FrozenConsensus,
    RepairCandidateSpec,
    rank_context_repair_candidates,
    rank_related_routes,
)


@dataclass(frozen=True)
class Q1ALNSConfig:
    iterations: int = 120
    time_limit_seconds: float = 900.0
    min_destroy_routes: int = 2
    max_destroy_routes: int = 3
    max_service_nodes: int = 2
    max_long_service_orders: int = 40
    repair_time_limit_seconds: float = 4.0
    initial_temperature: float = 0.002
    cooling_rate: float = 0.985
    reaction_factor: float = 0.25
    segment_length: int = 15
    related_destroy_mode: str = "legacy"
    frozen_consensus: FrozenConsensus | None = field(default=None, compare=False, repr=False)
    context_repair_mode: str = "none"
    context_candidate_budget: int = 0
    context_components: tuple[str, ...] = CONTEXT_COMPONENTS
    context_coverage_redundancy: int = 3
    stagnation_limit_seconds: float | None = None
    minimum_runtime_before_stagnation_stop: float = 0.0
    seed: int = 0

    def __post_init__(self) -> None:
        if self.iterations <= 0 or self.time_limit_seconds <= 0:
            raise ValueError("ALNS iteration and time limits must be positive")
        if not (2 <= self.min_destroy_routes <= self.max_destroy_routes):
            raise ValueError("Destroy size must satisfy 2 <= min <= max")
        if not (1 <= self.max_service_nodes <= 5):
            raise ValueError("max_service_nodes must be in [1, 5]")
        if self.max_long_service_orders <= 0:
            raise ValueError("max_long_service_orders must be positive")
        if not (0.0 < self.cooling_rate <= 1.0):
            raise ValueError("cooling_rate must be in (0, 1]")
        if not (0.0 < self.reaction_factor <= 1.0):
            raise ValueError("reaction_factor must be in (0, 1]")
        if self.related_destroy_mode not in {"legacy", "distance", "distance_consensus"}:
            raise ValueError(
                "related_destroy_mode must be legacy, distance, or distance_consensus"
            )
        if self.related_destroy_mode == "distance_consensus" and self.frozen_consensus is None:
            raise ValueError("distance_consensus mode requires frozen_consensus")
        if self.context_repair_mode not in {"none", "ranked"}:
            raise ValueError("context_repair_mode must be none or ranked")
        if self.context_repair_mode == "ranked" and self.context_candidate_budget <= 0:
            raise ValueError("ranked context repair requires a positive candidate budget")
        if not self.context_components or not set(self.context_components) <= set(CONTEXT_COMPONENTS):
            raise ValueError(f"context_components must be a subset of {CONTEXT_COMPONENTS}")
        if self.context_coverage_redundancy <= 0:
            raise ValueError("context_coverage_redundancy must be positive")
        if self.stagnation_limit_seconds is not None and self.stagnation_limit_seconds <= 0:
            raise ValueError("stagnation_limit_seconds must be positive when set")
        if self.minimum_runtime_before_stagnation_stop < 0:
            raise ValueError("minimum_runtime_before_stagnation_stop cannot be negative")


@dataclass(frozen=True)
class RouteVariant:
    base_airport: str
    aircraft_type: str
    service_order: tuple[str, ...]
    route: RoutePlan
    evaluation: RouteEvaluation
    capacity: int
    arrival_minutes: dict[str, int]

    @property
    def key(self) -> tuple[object, ...]:
        return (
            self.base_airport,
            self.aircraft_type,
            tuple((stop.facility_id, stop.refuel) for stop in self.route.stops),
            self.service_order,
        )


@dataclass
class _OperatorState:
    weight: float = 1.0
    calls: int = 0
    accepted: int = 0
    improved: int = 0
    best: int = 0
    segment_calls: int = 0
    segment_reward: float = 0.0
    feasible_repairs: int = 0
    failed_repairs: int = 0
    total_gain_minutes: int = 0
    runtime_seconds: float = 0.0
    destroyed_routes_total: int = 0


@dataclass(frozen=True)
class ALNSRunResult:
    solution: Solution
    convergence: tuple[dict[str, object], ...]
    operator_stats: tuple[dict[str, object], ...]
    weight_history: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class _VariantPoolResult:
    variants: list[RouteVariant]
    candidates_considered: int
    candidates_selected: int
    exact_candidate_builds: int


@dataclass(frozen=True)
class _RepairResult:
    routes: list[RoutePlan]
    evaluations: list[RouteEvaluation]
    variant_count: int
    candidates_considered: int
    candidates_selected: int
    exact_candidate_builds: int


def _solution_key(solution: Solution, order: tuple[str, ...]) -> tuple[float, ...]:
    return solution.metrics.comparison_key(order)


def _route_service_nodes(route: RoutePlan) -> tuple[str, ...]:
    if route.service_facilities:
        return tuple(dict.fromkeys(route.service_facilities))
    return tuple(dict.fromkeys(item.destination_id for item in route.assignments))


def _build_variant(
    data: ProblemData,
    base_airport: str,
    aircraft_type: str,
    service_order: tuple[str, ...],
    cache: dict[tuple[str, str, tuple[str, ...]], RouteVariant | None],
    solver_cache: SolverCache,
) -> RouteVariant | None:
    key = (base_airport, aircraft_type, service_order)
    if key in cache:
        return cache[key]
    augmented = solver_cache.augmentation_result(base_airport, aircraft_type, service_order)
    if not augmented.feasible:
        cache[key] = None
        return None
    route = RoutePlan(
        base_airport=base_airport,
        aircraft_type=aircraft_type,
        stops=augmented.stops,
        assignments=(),
        service_facilities=service_order,
    )
    evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
    if not evaluation.feasible:
        cache[key] = None
        return None

    aircraft = data.config.aircraft_types[aircraft_type]
    locations = tuple(stop.facility_id for stop in augmented.stops)
    arrivals: dict[str, int] = {}
    clock = 0
    for index, (origin, destination) in enumerate(zip(locations, locations[1:])):
        clock += flight_minutes(data.matrix[origin][destination], aircraft.speed_kmh)
        if destination in service_order and destination not in arrivals:
            arrivals[destination] = clock
        if index + 1 < len(locations) - 1:
            clock += minimum_stop_minutes(
                destination,
                augmented.stops[index + 1].refuel,
                data.config,
            )
    if set(arrivals) != set(service_order):
        cache[key] = None
        return None
    variant = RouteVariant(
        base_airport=base_airport,
        aircraft_type=aircraft_type,
        service_order=service_order,
        route=route,
        evaluation=evaluation,
        capacity=aircraft.seats,
        arrival_minutes=arrivals,
    )
    cache[key] = variant
    return variant


def _route_variant_from_existing(
    route: RoutePlan,
    evaluation: RouteEvaluation,
    data: ProblemData,
) -> RouteVariant:
    service_order = _route_service_nodes(route)
    locations = tuple(stop.facility_id for stop in route.stops)
    arrivals: dict[str, int] = {}
    clock = 0
    aircraft = data.config.aircraft_types[route.aircraft_type]
    for index, (origin, destination) in enumerate(zip(locations, locations[1:])):
        clock += flight_minutes(data.matrix[origin][destination], aircraft.speed_kmh)
        if destination in service_order and destination not in arrivals:
            arrivals[destination] = clock
        if index + 1 < len(locations) - 1:
            clock += minimum_stop_minutes(
                destination,
                route.stops[index + 1].refuel,
                data.config,
            )
    empty_route = RoutePlan(
        base_airport=route.base_airport,
        aircraft_type=route.aircraft_type,
        stops=route.stops,
        assignments=(),
        service_facilities=service_order,
    )
    empty_evaluation = evaluate_route(empty_route, matrix=data.matrix, config=data.config)
    return RouteVariant(
        base_airport=route.base_airport,
        aircraft_type=route.aircraft_type,
        service_order=service_order,
        route=empty_route,
        evaluation=empty_evaluation,
        capacity=aircraft.seats,
        arrival_minutes=arrivals,
    )


def _variant_pool(
    destroyed_routes: list[RoutePlan],
    destroyed_evaluations: list[RouteEvaluation],
    groups: dict[tuple[str, str], list[PassengerAssignment]],
    data: ProblemData,
    config: Q1ALNSConfig,
    cache: dict[tuple[str, str, tuple[str, ...]], RouteVariant | None],
    solver_cache: SolverCache,
) -> _VariantPoolResult:
    destinations = sorted({destination for _, destination in groups})
    variants: dict[tuple[object, ...], RouteVariant] = {}
    for route, evaluation in zip(destroyed_routes, destroyed_evaluations):
        existing = _route_variant_from_existing(route, evaluation, data)
        variants[existing.key] = existing

    candidate_specs: list[RepairCandidateSpec] = []
    maximum = min(config.max_service_nodes, len(destinations))
    for length in range(1, maximum + 1):
        orders = list(permutations(destinations, length))
        if length >= 3 and len(orders) > config.max_long_service_orders:
            orders.sort(
                key=lambda order: min(
                    sum(
                        data.matrix[left][right]
                        for left, right in zip(
                            (base_airport, *order),
                            (*order, base_airport),
                        )
                    )
                    for base_airport in data.config.airports
                )
            )
            orders = orders[: config.max_long_service_orders]
        for service_order in orders:
            for base_airport in data.config.airports:
                if not any(
                    destination in service_order
                    and (origin == "LAND" or origin == base_airport)
                    for origin, destination in groups
                ):
                    continue
                for aircraft_type in sorted(data.config.aircraft_types):
                    candidate_specs.append(
                        RepairCandidateSpec(
                            base_airport=base_airport,
                            aircraft_type=aircraft_type,
                            service_order=service_order,
                        )
                    )

    candidates_considered = len(candidate_specs)
    if config.context_repair_mode == "ranked":
        ordered, _ = rank_context_repair_candidates(
            candidate_specs,
            groups,
            destroyed_routes,
            data,
            components=config.context_components,
        )
        selected_keys = {
            candidate.key for candidate in ordered[: config.context_candidate_budget]
        }
        for origin, destination in sorted(groups):
            compatible = (
                candidate
                for candidate in ordered
                if destination in candidate.service_order
                and (origin == "LAND" or origin == candidate.base_airport)
            )
            for candidate in list(compatible)[: config.context_coverage_redundancy]:
                selected_keys.add(candidate.key)
        candidate_specs = [
            candidate for candidate in ordered if candidate.key in selected_keys
        ]

    exact_candidate_builds = 0
    for candidate in candidate_specs:
        if candidate.key not in cache:
            exact_candidate_builds += 1
        variant = _build_variant(
                        data,
                        candidate.base_airport,
                        candidate.aircraft_type,
                        candidate.service_order,
                        cache,
                        solver_cache,
                    )
        if variant is not None:
            variants[variant.key] = variant
    ordered_variants = sorted(
            variants.values(),
            key=lambda item: (
                item.evaluation.total_aircraft_time_minutes,
                item.base_airport,
                item.aircraft_type,
                item.service_order,
            ),
        )
    return _VariantPoolResult(
        variants=ordered_variants,
        candidates_considered=candidates_considered,
        candidates_selected=len(candidate_specs),
        exact_candidate_builds=exact_candidate_builds,
    )


def _solve_repair_milp(
    groups: dict[tuple[str, str], list[PassengerAssignment]],
    variants: list[RouteVariant],
    old_route_count: int,
    time_limit_seconds: float,
) -> tuple[np.ndarray, dict[tuple[int, int], int]] | None:
    group_keys = sorted(groups)
    group_index = {key: index for index, key in enumerate(group_keys)}
    y_count = len(variants)
    x_pairs: list[tuple[int, int]] = []
    for group_id, (origin, destination) in enumerate(group_keys):
        for variant_id, variant in enumerate(variants):
            if destination in variant.arrival_minutes and (
                origin == "LAND" or origin == variant.base_airport
            ):
                x_pairs.append((group_id, variant_id))
    if not x_pairs:
        return None
    x_offset = y_count
    x_column = {pair: x_offset + index for index, pair in enumerate(x_pairs)}
    variable_count = y_count + len(x_pairs)

    rows: list[int] = []
    columns: list[int] = []
    values: list[float] = []
    lower: list[float] = []
    upper: list[float] = []

    def add_constraint(coefficients: Iterable[tuple[int, float]], lo: float, hi: float) -> None:
        row = len(lower)
        for column, value in coefficients:
            rows.append(row)
            columns.append(column)
            values.append(value)
        lower.append(lo)
        upper.append(hi)

    for group_id, key in enumerate(group_keys):
        coefficients = [
            (x_column[(group_id, variant_id)], 1.0)
            for variant_id in range(y_count)
            if (group_id, variant_id) in x_column
        ]
        demand = len(groups[key])
        add_constraint(coefficients, float(demand), float(demand))

    for variant_id, variant in enumerate(variants):
        coefficients = [(variant_id, -float(variant.capacity))]
        coefficients.extend(
            (column, 1.0)
            for (group_id, other_variant), column in x_column.items()
            if other_variant == variant_id
        )
        add_constraint(coefficients, -np.inf, 0.0)
        for destination in variant.service_order:
            service_coefficients = [(variant_id, 1.0)]
            service_coefficients.extend(
                (column, -1.0)
                for (group_id, other_variant), column in x_column.items()
                if other_variant == variant_id and group_keys[group_id][1] == destination
            )
            add_constraint(service_coefficients, -np.inf, 0.0)

    matrix = coo_matrix(
        (np.asarray(values), (np.asarray(rows), np.asarray(columns))),
        shape=(len(lower), variable_count),
    ).tocsr()
    constraints: list[LinearConstraint] = [
        LinearConstraint(matrix, np.asarray(lower), np.asarray(upper))
    ]
    integrality = np.ones(variable_count, dtype=int)
    variable_lower = np.zeros(variable_count)
    variable_upper = np.empty(variable_count)
    maximum_flights = max(old_route_count + 1, math.ceil(sum(map(len, groups.values())) / 12) + 1)
    variable_upper[:y_count] = maximum_flights
    for pair, column in x_column.items():
        variable_upper[column] = len(groups[group_keys[pair[0]]])
    bounds = Bounds(variable_lower, variable_upper)

    primary = np.zeros(variable_count)
    for variant_id, variant in enumerate(variants):
        primary[variant_id] = variant.evaluation.total_aircraft_time_minutes
    options = {
        "time_limit": max(0.5, time_limit_seconds * 0.55),
        "mip_rel_gap": 0.0,
        "presolve": True,
    }
    first = milp(
        c=primary,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
        options=options,
    )
    if first.x is None:
        return None
    primary_optimum = round(float(primary @ first.x))
    time_row = coo_matrix(primary.reshape(1, -1)).tocsr()
    constraints.append(LinearConstraint(time_row, primary_optimum, primary_optimum))

    secondary = np.zeros(variable_count)
    for variant_id, variant in enumerate(variants):
        secondary[variant_id] = 1_000.0 + variant.evaluation.total_fuel_consumption_kg * 1e-4
    for (group_id, variant_id), column in x_column.items():
        destination = group_keys[group_id][1]
        secondary[column] = variants[variant_id].arrival_minutes[destination] * 1_000_000.0
    second = milp(
        c=secondary,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
        options={
            "time_limit": max(0.5, time_limit_seconds * 0.45),
            "mip_rel_gap": 0.0,
            "presolve": True,
        },
    )
    selected = second.x if second.x is not None else first.x
    y_values = np.rint(selected[:y_count]).astype(int)
    x_values = {
        pair: int(round(float(selected[column])))
        for pair, column in x_column.items()
        if selected[column] > 0.5
    }
    return y_values, x_values


def _materialize_repair(
    groups: dict[tuple[str, str], list[PassengerAssignment]],
    variants: list[RouteVariant],
    y_values: np.ndarray,
    x_values: dict[tuple[int, int], int],
    data: ProblemData,
) -> tuple[list[RoutePlan], list[RouteEvaluation]] | None:
    group_keys = sorted(groups)
    group_people = {key: list(values) for key, values in groups.items()}
    routes: list[RoutePlan] = []
    evaluations: list[RouteEvaluation] = []

    for variant_id, multiplicity in enumerate(y_values):
        if multiplicity <= 0:
            continue
        variant = variants[variant_id]
        selected: dict[str, list[PassengerAssignment]] = defaultdict(list)
        for group_id, key in enumerate(group_keys):
            count = x_values.get((group_id, variant_id), 0)
            if count:
                if count > len(group_people[key]):
                    return None
                picked = group_people[key][:count]
                del group_people[key][:count]
                selected[key[1]].extend(picked)

        bins: list[list[PassengerAssignment]] = [[] for _ in range(int(multiplicity))]
        for destination in variant.service_order:
            available = selected[destination]
            if len(available) < multiplicity:
                return None
            for bin_index in range(int(multiplicity)):
                bins[bin_index].append(available.pop())
        remaining = [item for destination in variant.service_order for item in selected[destination]]
        remaining.sort(key=lambda item: (item.destination_id, item.person_id))
        for item in remaining:
            target = min(range(len(bins)), key=lambda index: (len(bins[index]), index))
            if len(bins[target]) >= variant.capacity:
                return None
            bins[target].append(item)

        locations = tuple(stop.facility_id for stop in variant.route.stops)
        for passengers in bins:
            if not passengers or len(passengers) > variant.capacity:
                return None
            assignments = tuple(
                PassengerAssignment(
                    person_id=item.person_id,
                    origin_id=item.origin_id,
                    destination_id=item.destination_id,
                    pickup_stop_order=0,
                    delivery_stop_order=locations.index(item.destination_id, 1),
                )
                for item in sorted(passengers, key=lambda value: value.person_id)
            )
            route = RoutePlan(
                base_airport=variant.base_airport,
                aircraft_type=variant.aircraft_type,
                stops=variant.route.stops,
                assignments=assignments,
                service_facilities=variant.service_order,
            )
            evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
            if not evaluation.feasible:
                return None
            routes.append(route)
            evaluations.append(evaluation)

    if any(group_people.values()):
        return None
    return routes, evaluations


def _repair_neighborhood(
    destroyed_routes: list[RoutePlan],
    destroyed_evaluations: list[RouteEvaluation],
    data: ProblemData,
    config: Q1ALNSConfig,
    cache: dict[tuple[str, str, tuple[str, ...]], RouteVariant | None],
    solver_cache: SolverCache,
) -> _RepairResult | None:
    groups: dict[tuple[str, str], list[PassengerAssignment]] = defaultdict(list)
    for route in destroyed_routes:
        for assignment in route.assignments:
            groups[(assignment.origin_id, assignment.destination_id)].append(assignment)
    for values in groups.values():
        values.sort(key=lambda item: item.person_id)
    pool = _variant_pool(
        destroyed_routes,
        destroyed_evaluations,
        groups,
        data,
        config,
        cache,
        solver_cache,
    )
    solved = _solve_repair_milp(
        groups,
        pool.variants,
        len(destroyed_routes),
        config.repair_time_limit_seconds,
    )
    if solved is None:
        return None
    materialized = _materialize_repair(
        groups,
        pool.variants,
        solved[0],
        solved[1],
        data,
    )
    if materialized is None:
        return None
    return _RepairResult(
        routes=materialized[0],
        evaluations=materialized[1],
        variant_count=len(pool.variants),
        candidates_considered=pool.candidates_considered,
        candidates_selected=pool.candidates_selected,
        exact_candidate_builds=pool.exact_candidate_builds,
    )


def _route_relatedness(
    left: RoutePlan,
    right: RoutePlan,
    data: ProblemData,
    mode: str = "legacy",
) -> float:
    left_nodes = _route_service_nodes(left)
    right_nodes = _route_service_nodes(right)
    distance = min(data.matrix[a][b] for a in left_nodes for b in right_nodes)
    if mode in {"distance", "distance_consensus"}:
        return distance
    base_penalty = 0.0 if left.base_airport == right.base_airport else 35.0
    fixed_penalty = 0.0
    if any(item.origin_id != "LAND" for item in left.assignments + right.assignments):
        fixed_penalty = 15.0
    return distance + base_penalty + fixed_penalty


def _select_destroy_indices(
    operator: str,
    routes: list[RoutePlan],
    evaluations: list[RouteEvaluation],
    count: int,
    data: ProblemData,
    rng: random.Random,
    related_destroy_mode: str = "legacy",
    frozen_consensus: FrozenConsensus | None = None,
) -> list[int]:
    count = min(count, len(routes))
    if operator == "random_routes":
        return sorted(rng.sample(range(len(routes)), count))
    if operator == "related_routes":
        seed = rng.randrange(len(routes))
        candidates = [index for index in range(len(routes)) if index != seed]
        ranked = (
            sorted(
                candidates,
                key=lambda index: (
                    _route_relatedness(routes[seed], routes[index], data, "legacy"),
                    index,
                ),
            )
            if related_destroy_mode == "legacy"
            else rank_related_routes(
                routes[seed],
                candidates,
                routes,
                data,
                mode=related_destroy_mode,
                consensus=frozen_consensus,
            )
        )
        return sorted([seed, *ranked[: count - 1]])
    if operator == "low_utilization":
        ranked = sorted(
            range(len(routes)),
            key=lambda index: (
                evaluations[index].seat_utilization,
                -evaluations[index].total_aircraft_time_minutes,
                rng.random(),
            ),
        )
        window = ranked[: min(len(ranked), max(count * 3, count))]
        return sorted(rng.sample(window, count))
    if operator == "worst_time_per_person":
        ranked = sorted(
            range(len(routes)),
            key=lambda index: (
                -evaluations[index].total_aircraft_time_minutes
                / max(1, routes[index].passenger_count),
                rng.random(),
            ),
        )
        window = ranked[: min(len(ranked), max(count * 3, count))]
        return sorted(rng.sample(window, count))
    if operator == "land_reassignment":
        ranked = sorted(
            range(len(routes)),
            key=lambda index: (
                -sum(item.origin_id == "LAND" for item in routes[index].assignments),
                evaluations[index].seat_utilization,
                rng.random(),
            ),
        )
        seed = ranked[0]
        candidates = list(ranked[1:])
        related = (
            sorted(
                candidates,
                key=lambda index: (
                    _route_relatedness(routes[seed], routes[index], data, "legacy"),
                    index,
                ),
            )
            if related_destroy_mode == "legacy"
            else rank_related_routes(
                routes[seed],
                candidates,
                routes,
                data,
                mode=related_destroy_mode,
                consensus=frozen_consensus,
            )
        )
        return sorted([seed, *related[: count - 1]])
    raise ValueError(f"Unknown destroy operator: {operator}")


def _roulette(states: dict[str, _OperatorState], rng: random.Random) -> str:
    total = sum(state.weight for state in states.values())
    draw = rng.random() * total
    cumulative = 0.0
    for name in sorted(states):
        cumulative += states[name].weight
        if draw <= cumulative:
            return name
    return sorted(states)[-1]


def _relative_deterioration(candidate: Solution, current: Solution, order: tuple[str, ...]) -> float:
    candidate_key = _solution_key(candidate, order)
    current_key = _solution_key(current, order)
    for candidate_value, current_value in zip(candidate_key, current_key):
        if abs(candidate_value - current_value) > 1e-9:
            return max(0.0, (candidate_value - current_value) / max(1.0, abs(current_value)))
    return 0.0


def improve_q1_alns(
    solution: Solution,
    data: ProblemData,
    solver_config: SolverConfig | None = None,
    alns_config: Q1ALNSConfig | None = None,
    cache: SolverCache | None = None,
) -> ALNSRunResult:
    solver_config = solver_config or SolverConfig()
    alns_config = alns_config or Q1ALNSConfig(seed=solver_config.seed)
    solver_cache = cache or SolverCache(data)
    rng = random.Random(alns_config.seed)
    current_routes = list(solution.routes)
    current_evaluations = [
        evaluate_route(route, matrix=data.matrix, config=data.config) for route in current_routes
    ]
    if any(not evaluation.feasible for evaluation in current_evaluations):
        raise ValueError("ALNS input solution contains infeasible routes")
    current = Solution(
        routes=tuple(current_routes),
        metrics=aggregate_evaluations(current_evaluations, solution.metrics.served_passengers),
        method="q1_b2_alns",
        diagnostics=dict(solution.diagnostics),
    )
    best = current
    best_routes = list(current_routes)
    best_evaluations = list(current_evaluations)
    operators = {
        name: _OperatorState()
        for name in (
            "random_routes",
            "related_routes",
            "low_utilization",
            "worst_time_per_person",
            "land_reassignment",
        )
    }
    convergence: list[dict[str, object]] = []
    weight_history: list[dict[str, object]] = []
    variant_cache: dict[tuple[str, str, tuple[str, ...]], RouteVariant | None] = {}
    temperature = alns_config.initial_temperature
    started = time.perf_counter()
    last_best_elapsed = 0.0
    stop_reason = "iteration_limit"

    for iteration in range(1, alns_config.iterations + 1):
        elapsed = time.perf_counter() - started
        if elapsed >= alns_config.time_limit_seconds:
            stop_reason = "time_limit"
            break
        if (
            alns_config.stagnation_limit_seconds is not None
            and elapsed >= alns_config.minimum_runtime_before_stagnation_stop
            and elapsed - last_best_elapsed >= alns_config.stagnation_limit_seconds
        ):
            stop_reason = "stagnation"
            break
        operator = _roulette(operators, rng)
        state = operators[operator]
        state.calls += 1
        state.segment_calls += 1
        destroy_count = rng.randint(
            alns_config.min_destroy_routes,
            alns_config.max_destroy_routes,
        )
        indices = _select_destroy_indices(
            operator,
            current_routes,
            current_evaluations,
            destroy_count,
            data,
            rng,
            alns_config.related_destroy_mode,
            alns_config.frozen_consensus,
        )
        destroyed_routes = [current_routes[index] for index in indices]
        destroyed_evaluations = [current_evaluations[index] for index in indices]
        destroyed_passengers = sum(len(route.assignments) for route in destroyed_routes)
        removed_aircraft_time = sum(
            evaluation.total_aircraft_time_minutes for evaluation in destroyed_evaluations
        )
        state.destroyed_routes_total += len(indices)
        repair_started = time.perf_counter()
        repaired = _repair_neighborhood(
            destroyed_routes,
            destroyed_evaluations,
            data,
            alns_config,
            variant_cache,
            solver_cache,
        )
        state.runtime_seconds += time.perf_counter() - repair_started
        if repaired is None:
            state.failed_repairs += 1
        else:
            state.feasible_repairs += 1
        accepted = False
        improved = False
        global_best = False
        if repaired is not None:
            kept_routes = [
                route for index, route in enumerate(current_routes) if index not in set(indices)
            ]
            kept_evaluations = [
                value for index, value in enumerate(current_evaluations) if index not in set(indices)
            ]
            candidate_routes = kept_routes + repaired.routes
            candidate_evaluations = kept_evaluations + repaired.evaluations
            candidate = Solution(
                routes=tuple(candidate_routes),
                metrics=aggregate_evaluations(
                    candidate_evaluations,
                    solution.metrics.served_passengers,
                ),
                method="q1_b2_alns",
                diagnostics=dict(solution.diagnostics),
            )
            improved = _solution_key(candidate, solver_config.secondary_order) < _solution_key(
                current, solver_config.secondary_order
            )
            if improved:
                accepted = True
                state.total_gain_minutes += max(
                    0,
                    current.metrics.total_aircraft_time_minutes
                    - candidate.metrics.total_aircraft_time_minutes,
                )
            else:
                deterioration = _relative_deterioration(
                    candidate,
                    current,
                    solver_config.secondary_order,
                )
                probability = math.exp(-deterioration / max(temperature, 1e-12))
                accepted = rng.random() < probability
            if accepted:
                current = candidate
                current_routes = candidate_routes
                current_evaluations = candidate_evaluations
                state.accepted += 1
                if improved:
                    state.improved += 1
                    state.segment_reward += 4.0
                else:
                    state.segment_reward += 1.0
                if _solution_key(candidate, solver_config.secondary_order) < _solution_key(
                    best, solver_config.secondary_order
                ):
                    best = candidate
                    best_routes = list(candidate_routes)
                    best_evaluations = list(candidate_evaluations)
                    state.best += 1
                    state.segment_reward += 8.0
                    global_best = True
                    last_best_elapsed = time.perf_counter() - started

        convergence.append(
            {
                "iteration": iteration,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "operator": operator,
                "destroyed_routes": len(indices),
                "destroyed_passengers": destroyed_passengers,
                "removed_aircraft_time_minutes": removed_aircraft_time,
                "repair_variants": repaired.variant_count if repaired is not None else "",
                "repaired_routes": len(repaired.routes) if repaired is not None else "",
                "repair_candidates_considered": (
                    repaired.candidates_considered if repaired is not None else ""
                ),
                "repair_candidates_selected": (
                    repaired.candidates_selected if repaired is not None else ""
                ),
                "repair_exact_candidate_builds": (
                    repaired.exact_candidate_builds if repaired is not None else ""
                ),
                "accepted": int(accepted),
                "improved_current": int(improved),
                "new_global_best": int(global_best),
                "current_aircraft_time_minutes": current.metrics.total_aircraft_time_minutes,
                "best_aircraft_time_minutes": best.metrics.total_aircraft_time_minutes,
                "current_passenger_time_minutes": current.metrics.total_passenger_travel_time_minutes,
                "best_passenger_time_minutes": best.metrics.total_passenger_travel_time_minutes,
                "current_flights": current.metrics.total_flights,
                "best_flights": best.metrics.total_flights,
                "temperature": temperature,
            }
        )
        temperature *= alns_config.cooling_rate
        if iteration % alns_config.segment_length == 0:
            for operator_name, operator_state in operators.items():
                observed = operator_state.segment_reward / max(1, operator_state.segment_calls)
                operator_state.weight = max(
                    0.05,
                    (1.0 - alns_config.reaction_factor) * operator_state.weight
                    + alns_config.reaction_factor * observed,
                )
                operator_state.segment_calls = 0
                operator_state.segment_reward = 0.0
                weight_history.append(
                    {
                        "iteration": iteration,
                        "operator": operator_name,
                        "weight": round(operator_state.weight, 6),
                    }
                )

    final_metrics = aggregate_evaluations(best_evaluations, solution.metrics.served_passengers)
    diagnostics = {
        **solution.diagnostics,
        "alns": {
            "seed": alns_config.seed,
            "iterations_completed": len(convergence),
            "elapsed_seconds": round(time.perf_counter() - started, 6),
            "initial_aircraft_time_minutes": solution.metrics.total_aircraft_time_minutes,
            "best_aircraft_time_minutes": final_metrics.total_aircraft_time_minutes,
            "primary_improvement_minutes": (
                solution.metrics.total_aircraft_time_minutes
                - final_metrics.total_aircraft_time_minutes
            ),
            "variant_cache_size": len(variant_cache),
            "related_destroy_mode": alns_config.related_destroy_mode,
            "context_repair_mode": alns_config.context_repair_mode,
            "context_candidate_budget": alns_config.context_candidate_budget,
            "context_components": list(alns_config.context_components),
            "stagnation_limit_seconds": alns_config.stagnation_limit_seconds,
            "minimum_runtime_before_stagnation_stop": (
                alns_config.minimum_runtime_before_stagnation_stop
            ),
            "stop_reason": stop_reason,
        },
    }
    best_solution = Solution(
        routes=tuple(best_routes),
        metrics=final_metrics,
        method="q1_b2_alns",
        diagnostics=diagnostics,
    )
    operator_rows = tuple(
        {
            "operator": name,
            "weight": round(state.weight, 6),
            "calls": state.calls,
            "accepted": state.accepted,
            "improved": state.improved,
            "new_global_best": state.best,
            "feasible_repairs": state.feasible_repairs,
            "failed_repairs": state.failed_repairs,
            "total_gain_minutes": state.total_gain_minutes,
            "mean_gain_when_improving": round(
                state.total_gain_minutes / max(1, state.improved), 3
            ),
            "runtime_seconds": round(state.runtime_seconds, 3),
            "mean_destroyed_routes": round(
                state.destroyed_routes_total / max(1, state.calls), 3
            ),
        }
        for name, state in sorted(operators.items())
    )
    return ALNSRunResult(
        best_solution, tuple(convergence), operator_rows, tuple(weight_history)
    )
