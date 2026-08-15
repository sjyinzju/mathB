from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import permutations
from typing import Iterable, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from ..rules import flight_minutes, minimum_stop_minutes
from .data import ProblemData
from .evaluator import evaluate_route
from .models import (
    PassengerAssignment,
    RouteEvaluation,
    RoutePlan,
    Solution,
    aggregate_evaluations,
)
from .technical_stops import augment_service_sequence


@dataclass(frozen=True)
class Q2MasterConfig:
    nearest_neighbors: int = 5
    high_demand_nodes: int = 12
    time_limit_seconds: float = 300.0
    primary_fraction: float = 0.65
    mip_relative_gap: float = 0.0

    def __post_init__(self) -> None:
        if self.nearest_neighbors < 0 or self.high_demand_nodes < 0:
            raise ValueError("Candidate-pool sizes must be nonnegative")
        if self.time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be positive")
        if not 0.0 < self.primary_fraction < 1.0:
            raise ValueError("primary_fraction must be in (0, 1)")


@dataclass(frozen=True)
class Q2RouteVariant:
    base_airport: str
    aircraft_type: str
    service_order: tuple[str, ...]
    route: RoutePlan
    evaluation: RouteEvaluation
    capacity: int
    departures: tuple[int, ...]
    arrivals: tuple[int, ...]

    @property
    def key(self) -> tuple[object, ...]:
        return (
            self.base_airport,
            self.aircraft_type,
            tuple((stop.facility_id, int(stop.refuel)) for stop in self.route.stops),
            self.service_order,
        )


@dataclass(frozen=True)
class Q2MasterStats:
    demand_groups: int
    candidate_sequences: int
    candidate_variants: int
    compatible_assignments: int
    primary_status: int
    primary_objective: float
    primary_dual_bound: float | None
    primary_mip_gap: float | None
    primary_proven_optimal: bool
    secondary_status: int | None
    secondary_mip_gap: float | None
    secondary_proven_optimal: bool
    lexicographic_weights: dict[str, int]
    final_objectives: dict[str, float]
    elapsed_seconds: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def q2_direction(origin: str, destination: str, airports: Sequence[str]) -> str:
    airport_set = set(airports)
    if origin == "LAND" or origin in airport_set:
        return "outbound"
    if destination == "LAND" or destination in airport_set:
        return "inbound"
    return "shuttle"


def _variant_clock(
    route: RoutePlan,
    data: ProblemData,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    aircraft = data.config.aircraft_types[route.aircraft_type]
    locations = tuple(stop.facility_id for stop in route.stops)
    arrivals = [0] * len(locations)
    departures = [0] * len(locations)
    clock = 0
    for index, (origin, destination) in enumerate(zip(locations, locations[1:])):
        clock += flight_minutes(data.matrix[origin][destination], aircraft.speed_kmh)
        arrivals[index + 1] = clock
        if index + 1 < len(locations) - 1:
            clock += minimum_stop_minutes(
                destination,
                route.stops[index + 1].refuel,
                data.config,
            )
            departures[index + 1] = clock
    return tuple(departures), tuple(arrivals)


def build_q2_variant(
    data: ProblemData,
    base_airport: str,
    aircraft_type: str,
    service_order: tuple[str, ...],
) -> Q2RouteVariant | None:
    if not service_order or len(set(service_order)) != len(service_order):
        return None
    augmented = augment_service_sequence(
        base_airport,
        aircraft_type,
        service_order,
        matrix=data.matrix,
        config=data.config,
    )
    if not augmented.feasible:
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
        return None
    departures, arrivals = _variant_clock(route, data)
    return Q2RouteVariant(
        base_airport=base_airport,
        aircraft_type=aircraft_type,
        service_order=service_order,
        route=route,
        evaluation=evaluation,
        capacity=data.config.aircraft_types[aircraft_type].seats,
        departures=departures,
        arrivals=arrivals,
    )


def assignment_interval(
    variant: Q2RouteVariant,
    origin: str,
    destination: str,
    airports: Sequence[str],
) -> tuple[int, int, int] | None:
    airport_set = set(airports)
    locations = tuple(stop.facility_id for stop in variant.route.stops)
    if origin == "LAND":
        pickup = 0
    elif origin in airport_set:
        if origin != variant.base_airport:
            return None
        pickup = 0
    else:
        pickup = next(
            (index for index, node in enumerate(locations[:-1]) if node == origin),
            -1,
        )
        if pickup < 1:
            return None

    if destination == "LAND":
        delivery = len(locations) - 1
    elif destination in airport_set:
        if destination != variant.base_airport:
            return None
        delivery = len(locations) - 1
    else:
        delivery = next(
            (
                index
                for index in range(pickup + 1, len(locations))
                if locations[index] == destination
            ),
            -1,
        )
        if delivery < 1:
            return None
    if pickup >= delivery:
        return None
    passenger_minutes = variant.arrivals[delivery] - variant.departures[pickup]
    return pickup, delivery, passenger_minutes


def candidate_service_sequences(
    data: ProblemData,
    *,
    seed_routes: Iterable[RoutePlan] = (),
    nearest_neighbors: int = 5,
    high_demand_nodes: int = 12,
    extra_sequences: Iterable[tuple[str, ...]] = (),
) -> tuple[tuple[str, ...], ...]:
    node_demand: dict[str, int] = defaultdict(int)
    sequences: set[tuple[str, ...]] = set()
    for pool in data.q2_pools.values():
        if pool.origin_id in data.config.facilities:
            node_demand[pool.origin_id] += pool.quantity
        if pool.destination_id in data.config.facilities:
            node_demand[pool.destination_id] += pool.quantity
    nodes = sorted(node_demand)
    sequences.update((node,) for node in nodes)

    for origin, destination in data.q2_pools:
        if origin in data.config.facilities and destination in data.config.facilities:
            sequences.add((origin, destination))

    for node in nodes:
        nearest = sorted(
            (other for other in nodes if other != node),
            key=lambda other: (data.matrix[node][other], other),
        )[:nearest_neighbors]
        for other in nearest:
            sequences.add((node, other))
            sequences.add((other, node))

    high = sorted(nodes, key=lambda node: (-node_demand[node], node))[:high_demand_nodes]
    sequences.update(permutations(high, 2))

    for route in seed_routes:
        order = tuple(dict.fromkeys(route.service_facilities))
        if not order:
            locations = tuple(stop.facility_id for stop in route.stops[1:-1])
            order = tuple(dict.fromkeys(locations))
        if 1 <= len(order) <= data.config.max_sea_landings:
            sequences.add(order)
            if len(order) == 2:
                sequences.add(tuple(reversed(order)))

    for sequence in extra_sequences:
        order = tuple(sequence)
        if (
            1 <= len(order) <= data.config.max_sea_landings
            and len(set(order)) == len(order)
            and all(node in data.config.facilities for node in order)
        ):
            sequences.add(order)
    return tuple(sorted(sequences, key=lambda item: (len(item), item)))


def adaptive_triple_sequences(
    data: ProblemData,
    seed_routes: Iterable[RoutePlan],
    *,
    limit: int = 40,
) -> tuple[tuple[str, ...], ...]:
    """Generate promising three-service-node columns around the current Q2 solution."""
    if limit <= 0:
        return ()
    candidates: set[tuple[str, ...]] = set()
    shuttle_edges = {
        (origin, destination)
        for origin, destination in data.q2_pools
        if origin in data.config.facilities and destination in data.config.facilities
    }
    outgoing: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, set[str]] = defaultdict(set)
    for origin, destination in shuttle_edges:
        outgoing[origin].add(destination)
        incoming[destination].add(origin)
    for middle in data.config.facilities:
        for origin in incoming.get(middle, set()):
            for destination in outgoing.get(middle, set()):
                if len({origin, middle, destination}) == 3:
                    candidates.add((origin, middle, destination))

    active_nodes: set[str] = set()
    for route in seed_routes:
        order = tuple(dict.fromkeys(route.service_facilities))
        active_nodes.update(order)
        if len(order) != 2:
            continue
        first, second = order
        related = set(outgoing.get(second, set())) | set(incoming.get(first, set()))
        related.update(
            sorted(
                (
                    node
                    for node in data.config.facilities
                    if node not in {first, second}
                ),
                key=lambda node: (
                    min(data.matrix[first][node], data.matrix[second][node]),
                    node,
                ),
            )[:3]
        )
        for node in related:
            if node in {first, second}:
                continue
            candidates.add((node, first, second))
            candidates.add((first, node, second))
            candidates.add((first, second, node))

    node_volume: dict[str, int] = defaultdict(int)
    edge_volume: dict[tuple[str, str], int] = defaultdict(int)
    for key, pool in data.q2_pools.items():
        origin, destination = key
        if origin in data.config.facilities:
            node_volume[origin] += pool.quantity
        if destination in data.config.facilities:
            node_volume[destination] += pool.quantity
        if origin in data.config.facilities and destination in data.config.facilities:
            edge_volume[(origin, destination)] += pool.quantity

    def score(sequence: tuple[str, ...]) -> tuple[float, float, tuple[str, ...]]:
        first, middle, last = sequence
        distance = min(
            data.matrix[base][first]
            + data.matrix[first][middle]
            + data.matrix[middle][last]
            + data.matrix[last][base]
            for base in data.config.airports
        )
        flow = (
            node_volume[first]
            + node_volume[middle]
            + node_volume[last]
            + 4 * edge_volume[(first, middle)]
            + 4 * edge_volume[(middle, last)]
            + 2 * edge_volume[(first, last)]
        )
        active_bonus = sum(node in active_nodes for node in sequence)
        return (distance / max(1, flow + 10 * active_bonus), distance, sequence)

    return tuple(sorted(candidates, key=score)[:limit])


def build_q2_variant_pool(
    data: ProblemData,
    sequences: Iterable[tuple[str, ...]],
    *,
    cache: dict[tuple[str, str, tuple[str, ...]], Q2RouteVariant | None] | None = None,
    group_keys: Iterable[tuple[str, str]] | None = None,
) -> tuple[Q2RouteVariant, ...]:
    cache = cache if cache is not None else {}
    keys = tuple(group_keys if group_keys is not None else data.q2_pools)
    variants: dict[tuple[object, ...], Q2RouteVariant] = {}
    for sequence in sequences:
        for base in data.config.airports:
            for aircraft_type in sorted(data.config.aircraft_types):
                key = (base, aircraft_type, tuple(sequence))
                if key not in cache:
                    cache[key] = build_q2_variant(data, base, aircraft_type, tuple(sequence))
                variant = cache[key]
                if variant is None:
                    continue
                if not any(
                    assignment_interval(variant, origin, destination, data.config.airports)
                    is not None
                    for origin, destination in keys
                ):
                    continue
                variants[variant.key] = variant
    return tuple(
        sorted(
            variants.values(),
            key=lambda item: (
                item.evaluation.total_aircraft_time_minutes,
                item.base_airport,
                item.aircraft_type,
                item.service_order,
            ),
        )
    )


def _solve_master_arrays(
    data: ProblemData,
    variants: Sequence[Q2RouteVariant],
    group_keys: Sequence[tuple[str, str]],
    config: Q2MasterConfig,
) -> tuple[np.ndarray, dict[tuple[int, int], int], Q2MasterStats] | None:
    started = time.perf_counter()
    y_count = len(variants)
    compatible: dict[tuple[int, int], tuple[int, int, int]] = {}
    by_variant: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
    for group_id, (origin, destination) in enumerate(group_keys):
        for variant_id, variant in enumerate(variants):
            interval = assignment_interval(
                variant, origin, destination, data.config.airports
            )
            if interval is None:
                continue
            compatible[(group_id, variant_id)] = interval
            by_variant[variant_id].append((group_id, *interval))
    if any(
        not any(pair[0] == group_id for pair in compatible)
        for group_id in range(len(group_keys))
    ):
        return None

    x_pairs = sorted(compatible)
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
        demand = data.q2_pools[key].quantity
        add_constraint(coefficients, float(demand), float(demand))

    for variant_id, variant in enumerate(variants):
        leg_count = len(variant.route.stops) - 1
        for leg in range(leg_count):
            coefficients: list[tuple[int, float]] = [
                (variant_id, -float(variant.capacity))
            ]
            coefficients.extend(
                (x_column[(group_id, variant_id)], 1.0)
                for group_id, pickup, delivery, _ in by_variant.get(variant_id, [])
                if pickup <= leg < delivery
            )
            add_constraint(coefficients, -np.inf, 0.0)

    matrix = coo_matrix(
        (np.asarray(values), (np.asarray(rows), np.asarray(columns))),
        shape=(len(lower), variable_count),
    ).tocsr()
    constraints: list[LinearConstraint] = [
        LinearConstraint(matrix, np.asarray(lower), np.asarray(upper))
    ]
    maximum_flights = math.ceil(
        sum(data.q2_pools[key].quantity for key in group_keys) / 12
    ) + 10
    variable_lower = np.zeros(variable_count)
    variable_upper = np.empty(variable_count)
    variable_upper[:y_count] = maximum_flights
    for (group_id, _), column in x_column.items():
        variable_upper[column] = data.q2_pools[group_keys[group_id]].quantity
    bounds = Bounds(variable_lower, variable_upper)
    integrality = np.ones(variable_count, dtype=np.uint8)

    primary = np.zeros(variable_count)
    passenger = np.zeros(variable_count)
    flights = np.zeros(variable_count)
    fuel_deci_kg = np.zeros(variable_count)
    for variant_id, variant in enumerate(variants):
        primary[variant_id] = variant.evaluation.total_aircraft_time_minutes
        flights[variant_id] = 1.0
        # Fuel is represented in 0.1 kg units so that every lexicographic
        # equality added below has integer coefficients and an integer RHS.
        fuel_deci_kg[variant_id] = round(
            variant.evaluation.total_fuel_consumption_kg * 10.0
        )
    for pair, column in x_column.items():
        passenger[column] = compatible[pair][2]

    first = milp(
        c=primary,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
        options={
            "time_limit": max(1.0, config.time_limit_seconds * config.primary_fraction),
            "mip_rel_gap": config.mip_relative_gap,
            "presolve": True,
        },
    )
    if first.x is None:
        return None
    primary_value = int(round(float(primary @ first.x)))
    constraints.append(
        LinearConstraint(
            coo_matrix(primary.reshape(1, -1)).tocsr(),
            float(primary_value),
            float(primary_value),
        )
    )

    # Under the equality sum(T_c y_c)=primary_value, the following bounds are
    # rigorous. They allow one mixed-integer objective to encode the remaining
    # passenger-time -> flight-count -> fuel order without hand-picked weights.
    positive_times = primary[:y_count][primary[:y_count] > 0]
    max_flights_at_primary = int(primary_value // float(positive_times.min()))
    max_fuel_rate = max(
        fuel_deci_kg[index] / primary[index]
        for index in range(y_count)
        if primary[index] > 0
    )
    max_fuel_deci_at_primary = int(math.ceil(primary_value * max_fuel_rate))
    flight_weight = max_fuel_deci_at_primary + 1
    passenger_weight = (
        max_flights_at_primary * flight_weight
        + max_fuel_deci_at_primary
        + 1
    )
    secondary = (
        passenger * passenger_weight
        + flights * flight_weight
        + fuel_deci_kg
    )
    second = milp(
        c=secondary,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
        options={
            "time_limit": max(
                1.0, config.time_limit_seconds * (1.0 - config.primary_fraction)
            ),
            "mip_rel_gap": config.mip_relative_gap,
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
    stats = Q2MasterStats(
        demand_groups=len(group_keys),
        candidate_sequences=len({variant.service_order for variant in variants}),
        candidate_variants=len(variants),
        compatible_assignments=len(x_pairs),
        primary_status=int(first.status),
        primary_objective=float(primary_value),
        primary_dual_bound=(
            float(first.mip_dual_bound)
            if getattr(first, "mip_dual_bound", None) is not None
            else None
        ),
        primary_mip_gap=(
            float(first.mip_gap) if getattr(first, "mip_gap", None) is not None else None
        ),
        primary_proven_optimal=bool(int(first.status) == 0),
        secondary_status=int(second.status),
        secondary_mip_gap=(
            float(second.mip_gap)
            if getattr(second, "mip_gap", None) is not None
            else None
        ),
        secondary_proven_optimal=bool(int(second.status) == 0),
        lexicographic_weights={
            "passenger_time": passenger_weight,
            "flights": flight_weight,
            "fuel_deci_kg": 1,
        },
        final_objectives={
            "aircraft_time_minutes": float(primary @ selected),
            "passenger_time_minutes": float(passenger @ selected),
            "flights": float(flights @ selected),
            "fuel_kg": round(float(fuel_deci_kg @ selected) / 10.0, 6),
        },
        elapsed_seconds=round(time.perf_counter() - started, 6),
    )
    return y_values, x_values, stats


def _materialize_master(
    data: ProblemData,
    variants: Sequence[Q2RouteVariant],
    group_keys: Sequence[tuple[str, str]],
    y_values: np.ndarray,
    x_values: dict[tuple[int, int], int],
    *,
    method: str,
    diagnostics: dict[str, object],
) -> Solution:
    remaining = {
        key: list(data.q2_pools[key].person_ids)
        for key in group_keys
    }
    routes: list[RoutePlan] = []
    evaluations: list[RouteEvaluation] = []

    for variant_id, multiplicity in enumerate(y_values):
        if multiplicity <= 0:
            continue
        variant = variants[variant_id]
        entries: list[tuple[int, int, PassengerAssignment]] = []
        locations = tuple(stop.facility_id for stop in variant.route.stops)
        for group_id, key in enumerate(group_keys):
            count = x_values.get((group_id, variant_id), 0)
            if count <= 0:
                continue
            people = remaining[key]
            if count > len(people):
                raise ValueError(f"Q2 materialization overuses group {key}")
            selected_people = people[:count]
            del people[:count]
            interval = assignment_interval(
                variant, key[0], key[1], data.config.airports
            )
            if interval is None:
                raise ValueError(f"Lost Q2 compatibility for {key}")
            pickup, delivery, _ = interval
            for person_id in selected_people:
                entries.append(
                    (
                        pickup,
                        delivery,
                        PassengerAssignment(
                            person_id=person_id,
                            origin_id=key[0],
                            destination_id=key[1],
                            pickup_stop_order=pickup,
                            delivery_stop_order=delivery,
                        ),
                    )
                )

        track_ends: list[int] = []
        track_entries: list[list[PassengerAssignment]] = []
        for pickup, delivery, assignment in sorted(
            entries,
            key=lambda item: (item[0], item[1], item[2].person_id),
        ):
            reusable = [
                index for index, end in enumerate(track_ends) if end <= pickup
            ]
            if reusable:
                track = max(reusable, key=lambda index: (track_ends[index], -index))
                track_ends[track] = delivery
                track_entries[track].append(assignment)
            else:
                track_ends.append(delivery)
                track_entries.append([assignment])
        if len(track_entries) > variant.capacity * int(multiplicity):
            raise ValueError("Aggregated Q2 capacity could not be decomposed into flights")

        flight_entries: list[list[PassengerAssignment]] = [
            [] for _ in range(int(multiplicity))
        ]
        for track, assignments in enumerate(track_entries):
            flight_entries[track // variant.capacity].extend(assignments)
        if any(not values for values in flight_entries):
            raise ValueError("Q2 master selected an empty route copy")
        for values in flight_entries:
            route = RoutePlan(
                base_airport=variant.base_airport,
                aircraft_type=variant.aircraft_type,
                stops=variant.route.stops,
                assignments=tuple(sorted(values, key=lambda item: item.person_id)),
                service_facilities=variant.service_order,
            )
            evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
            if not evaluation.feasible:
                raise ValueError(
                    f"Materialized Q2 route is infeasible: {evaluation.issues}; {locations}"
                )
            routes.append(route)
            evaluations.append(evaluation)

    leftovers = {key: len(values) for key, values in remaining.items() if values}
    if leftovers:
        raise ValueError(f"Q2 master left demand unassigned: {leftovers}")
    served = sum(len(route.assignments) for route in routes)
    return Solution(
        routes=tuple(routes),
        metrics=aggregate_evaluations(evaluations, served),
        method=method,
        diagnostics=diagnostics,
    )


def solve_q2_master(
    data: ProblemData,
    variants: Sequence[Q2RouteVariant],
    *,
    group_keys: Iterable[tuple[str, str]] | None = None,
    config: Q2MasterConfig | None = None,
    method: str = "q2_joint_route_master",
) -> Solution:
    config = config or Q2MasterConfig()
    keys = tuple(sorted(group_keys if group_keys is not None else data.q2_pools))
    solved = _solve_master_arrays(data, variants, keys, config)
    if solved is None:
        raise RuntimeError("Q2 candidate-route master did not return a feasible solution")
    y_values, x_values, stats = solved
    return _materialize_master(
        data,
        variants,
        keys,
        y_values,
        x_values,
        method=method,
        diagnostics={"q2_master": stats.to_dict()},
    )


def build_separate_q2_baseline(
    data: ProblemData,
    variants: Sequence[Q2RouteVariant],
    *,
    config: Q2MasterConfig | None = None,
) -> Solution:
    config = config or Q2MasterConfig()
    component_solutions: list[Solution] = []
    for direction in ("outbound", "inbound", "shuttle"):
        keys = [
            key
            for key in data.q2_pools
            if q2_direction(key[0], key[1], data.config.airports) == direction
        ]
        component_solutions.append(
            solve_q2_master(
                data,
                variants,
                group_keys=keys,
                config=config,
                method=f"q2_separate_{direction}",
            )
        )
    routes = tuple(route for solution in component_solutions for route in solution.routes)
    evaluations = [
        evaluate_route(route, matrix=data.matrix, config=data.config) for route in routes
    ]
    if any(not evaluation.feasible for evaluation in evaluations):
        raise ValueError("Separate Q2 baseline contains infeasible routes")
    return Solution(
        routes=routes,
        metrics=aggregate_evaluations(evaluations, data.q2_passenger_count),
        method="q2_b0_separate_transport",
        diagnostics={
            "components": [
                {"method": solution.method, "metrics": solution.metrics.to_dict()}
                for solution in component_solutions
            ]
        },
    )
