from __future__ import annotations

import math
import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from itertools import permutations
from statistics import mean
from typing import Iterable, Sequence

from .cache import SolverCache
from .data import ProblemData
from .evaluator import evaluate_route
from .models import DemandPool, RoutePlan, Solution, aggregate_evaluations
from .q2 import (
    Q2MasterConfig,
    build_q2_variant_pool,
    candidate_service_sequences,
    solve_q2_master,
)


DESTROY_OPERATORS = (
    "high_cost_route",
    "low_utilization_route",
    "shared_facility_flow",
    "land_heavy_route",
)


@dataclass(frozen=True)
class Q2LnsConfig:
    iterations: int = 24
    neighborhood_size: int = 3
    source_pool_size: int = 24
    target_pool_size: int = 8
    max_sequence_length: int = 2
    candidate_sequence_budget: int = 24
    local_primary_seconds: float = 4.0
    local_secondary_seconds: float = 1.0
    seed: int = 0
    operators: tuple[str, ...] = DESTROY_OPERATORS

    def __post_init__(self) -> None:
        if self.iterations < 0:
            raise ValueError("iterations must be nonnegative")
        if self.neighborhood_size < 2:
            raise ValueError("neighborhood_size must include a source and a target")
        if self.source_pool_size <= 0 or self.target_pool_size <= 0:
            raise ValueError("source and target pool sizes must be positive")
        if not 1 <= self.max_sequence_length <= 5:
            raise ValueError("max_sequence_length must be between one and five")
        if self.candidate_sequence_budget <= 0:
            raise ValueError("candidate_sequence_budget must be positive")
        if self.local_primary_seconds <= 0 or self.local_secondary_seconds < 0:
            raise ValueError("local MILP time limits are invalid")
        unknown = set(self.operators) - set(DESTROY_OPERATORS)
        if unknown:
            raise ValueError(f"Unknown Q2 destroy operators: {sorted(unknown)}")
        if not self.operators:
            raise ValueError("At least one destroy operator is required")


@dataclass(frozen=True)
class Q2LocalRepair:
    solution: Solution | None
    diagnostics: dict[str, object]


@dataclass(frozen=True)
class Q2LnsResult:
    solution: Solution
    iteration_log: tuple[dict[str, object], ...]
    operator_stats: tuple[dict[str, object], ...]
    elapsed_seconds: float


def _route_evaluation(route: RoutePlan, data: ProblemData):
    return evaluate_route(route, matrix=data.matrix, config=data.config)


def _route_facilities(route: RoutePlan, data: ProblemData) -> frozenset[str]:
    facilities = set(route.service_facilities)
    for assignment in route.assignments:
        if assignment.origin_id in data.config.facilities:
            facilities.add(assignment.origin_id)
        if assignment.destination_id in data.config.facilities:
            facilities.add(assignment.destination_id)
    return frozenset(facilities)


def _land_share(route: RoutePlan) -> float:
    if not route.assignments:
        return 0.0
    land = sum(
        assignment.origin_id == "LAND" or assignment.destination_id == "LAND"
        for assignment in route.assignments
    )
    return land / len(route.assignments)


def _source_order(
    solution: Solution,
    data: ProblemData,
    operator: str,
) -> list[int]:
    evaluations = [_route_evaluation(route, data) for route in solution.routes]
    if operator == "high_cost_route":
        key = lambda index: (
            -evaluations[index].total_aircraft_time_minutes,
            evaluations[index].seat_utilization,
            index,
        )
    elif operator == "low_utilization_route":
        key = lambda index: (
            evaluations[index].seat_utilization,
            -evaluations[index].total_aircraft_time_minutes,
            index,
        )
    elif operator == "shared_facility_flow":
        frequency = Counter(
            facility
            for route in solution.routes
            for facility in _route_facilities(route, data)
        )
        key = lambda index: (
            -sum(frequency[node] for node in _route_facilities(solution.routes[index], data)),
            -len(_route_facilities(solution.routes[index], data)),
            index,
        )
    elif operator == "land_heavy_route":
        key = lambda index: (
            -_land_share(solution.routes[index]),
            evaluations[index].seat_utilization,
            index,
        )
    else:  # protected by Q2LnsConfig validation
        raise ValueError(operator)
    return sorted(range(len(solution.routes)), key=key)


def _flow_between(
    left: frozenset[str],
    right: frozenset[str],
    data: ProblemData,
) -> int:
    return sum(
        pool.quantity
        for (origin, destination), pool in data.q2_pools.items()
        if (origin in left and destination in right)
        or (origin in right and destination in left)
    )


def _target_order(
    solution: Solution,
    data: ProblemData,
    source_index: int,
) -> list[int]:
    source = solution.routes[source_index]
    source_facilities = _route_facilities(source, data)
    evaluations = [_route_evaluation(route, data) for route in solution.routes]

    def key(index: int) -> tuple[float, ...]:
        target = solution.routes[index]
        target_facilities = _route_facilities(target, data)
        shared = len(source_facilities & target_facilities)
        flow = _flow_between(source_facilities, target_facilities, data)
        minimum_distance = min(
            (
                data.matrix[left][right]
                for left in source_facilities
                for right in target_facilities
            ),
            default=math.inf,
        )
        return (
            -float(shared),
            -float(flow),
            -float(source.base_airport == target.base_airport),
            minimum_distance,
            evaluations[index].seat_utilization,
            float(index),
        )

    return sorted(
        (index for index in range(len(solution.routes)) if index != source_index),
        key=key,
    )


def select_q2_neighborhood(
    solution: Solution,
    data: ProblemData,
    *,
    operator: str,
    iteration: int,
    config: Q2LnsConfig,
) -> tuple[int, ...]:
    """Select one source route and related targets reproducibly for a seed."""
    sources = _source_order(solution, data, operator)[: config.source_pool_size]
    source_slot = (iteration // len(config.operators) + config.seed) % len(sources)
    source = sources[source_slot]
    targets = _target_order(solution, data, source)[: config.target_pool_size]
    rng = random.Random((config.seed + 1) * 1_000_003 + iteration * 97)
    # Preserve the strongest related target and diversify the remaining slots.
    selected = targets[:1]
    tail = targets[1:]
    rng.shuffle(tail)
    selected.extend(tail[: max(0, config.neighborhood_size - 2)])
    if len(selected) < config.neighborhood_size - 1:
        selected.extend(
            target for target in targets if target not in selected
        )
    return tuple([source, *selected[: config.neighborhood_size - 1]])


def build_q2_local_data(
    data: ProblemData,
    routes: Iterable[RoutePlan],
) -> ProblemData:
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for route in routes:
        for assignment in route.assignments:
            grouped[(assignment.origin_id, assignment.destination_id)].append(
                assignment.person_id
            )
    pools = {
        key: DemandPool(key[0], key[1], tuple(sorted(person_ids)))
        for key, person_ids in sorted(grouped.items())
    }
    return replace(data, q2_pools=pools)


def _sequence_supports_local_demand(
    sequence: tuple[str, ...],
    data: ProblemData,
) -> bool:
    positions = {node: index for index, node in enumerate(sequence)}
    airports = set(data.config.airports)
    for origin, destination in data.q2_pools:
        origin_ok = origin == "LAND" or origin in airports or origin in positions
        destination_ok = (
            destination == "LAND" or destination in airports or destination in positions
        )
        if not origin_ok or not destination_ok:
            continue
        if origin in positions and destination in positions:
            if positions[origin] >= positions[destination]:
                continue
        return True
    return False


def _geometry_score(sequence: tuple[str, ...], data: ProblemData) -> tuple[float, ...]:
    best = math.inf
    for base in data.config.airports:
        distance = data.matrix[base][sequence[0]]
        distance += sum(data.matrix[left][right] for left, right in zip(sequence, sequence[1:]))
        distance += data.matrix[sequence[-1]][base]
        best = min(best, distance)
    return (best, float(len(sequence)), *sequence)


def geometry_local_sequences(
    data: ProblemData,
    routes: Sequence[RoutePlan],
    *,
    max_sequence_length: int,
    budget: int,
) -> tuple[tuple[str, ...], ...]:
    """Bounded raw/geometry candidate control for local exact repair."""
    facilities = sorted(
        {
            node
            for route in routes
            for node in _route_facilities(route, data)
        }
    )
    required = {
        tuple(route.service_facilities)
        for route in routes
        if route.service_facilities
        and len(set(route.service_facilities)) == len(route.service_facilities)
    }
    base = set(
        candidate_service_sequences(
            data,
            seed_routes=routes,
            nearest_neighbors=0,
            high_demand_nodes=0,
        )
    )
    generated: list[tuple[str, ...]] = []
    for length in range(2, min(max_sequence_length, len(facilities)) + 1):
        generated.extend(
            sequence
            for sequence in permutations(facilities, length)
            if _sequence_supports_local_demand(sequence, data)
        )
    ranked = sorted(set(generated) - required - base, key=lambda item: _geometry_score(item, data))
    room = max(0, budget - len(required) - len(base))
    chosen = required | base | set(ranked[:room])
    # Required/current sequences are never dropped even if they exceed the
    # exploratory budget; this preserves a feasible incumbent representation.
    return tuple(sorted(chosen, key=lambda item: (len(item), item)))


def _combine_solution(
    current: Solution,
    repaired: Solution,
    destroyed: frozenset[int],
    data: ProblemData,
    *,
    diagnostics: dict[str, object],
) -> Solution:
    routes = tuple(
        route for index, route in enumerate(current.routes) if index not in destroyed
    ) + repaired.routes
    people = [assignment.person_id for route in routes for assignment in route.assignments]
    if len(people) != len(set(people)) or len(people) != data.q2_passenger_count:
        raise ValueError("Q2 local repair did not preserve exact passenger coverage")
    evaluations = [_route_evaluation(route, data) for route in routes]
    if any(not evaluation.feasible for evaluation in evaluations):
        raise ValueError("Q2 local repair produced an infeasible complete solution")
    return Solution(
        routes=routes,
        metrics=aggregate_evaluations(evaluations, len(people)),
        method="q2_lns_exact_local_repair",
        diagnostics=diagnostics,
    )


def exact_q2_local_repair(
    current: Solution,
    data: ProblemData,
    route_indices: Iterable[int],
    *,
    cache: SolverCache,
    config: Q2LnsConfig,
) -> Q2LocalRepair:
    started = time.perf_counter()
    destroyed = tuple(sorted(set(route_indices)))
    if len(destroyed) < 2 or destroyed[-1] >= len(current.routes):
        raise ValueError("Invalid Q2 local-repair route indices")
    affected_routes = tuple(current.routes[index] for index in destroyed)
    local_data = build_q2_local_data(data, affected_routes)
    sequences = geometry_local_sequences(
        local_data,
        affected_routes,
        max_sequence_length=config.max_sequence_length,
        budget=config.candidate_sequence_budget,
    )
    variants = build_q2_variant_pool(
        local_data,
        sequences,
        cache=cache,
        group_keys=local_data.q2_pools,
    )
    before_evaluations = [_route_evaluation(route, data) for route in affected_routes]
    before_aircraft = sum(item.total_aircraft_time_minutes for item in before_evaluations)
    before_passenger = sum(item.total_passenger_travel_time_minutes for item in before_evaluations)
    base_diagnostics: dict[str, object] = {
        "destroyed_routes": list(destroyed),
        "affected_people": local_data.q2_passenger_count,
        "affected_demand_groups": len(local_data.q2_pools),
        "facilities": sorted(
            {node for route in affected_routes for node in _route_facilities(route, data)}
        ),
        "candidate_sequences": len(sequences),
        "candidate_variants": len(variants),
        "before_routes": len(affected_routes),
        "before_aircraft_minutes": before_aircraft,
        "before_passenger_minutes": before_passenger,
    }
    if not variants:
        return Q2LocalRepair(
            None,
            {**base_diagnostics, "repair_success": False, "reason": "no_variants"},
        )
    try:
        local_solution = solve_q2_master(
            local_data,
            variants,
            config=Q2MasterConfig(
                nearest_neighbors=0,
                high_demand_nodes=0,
                primary_time_limit_seconds=config.local_primary_seconds,
                secondary_time_limit_seconds=config.local_secondary_seconds,
                primary_upper_bound_minutes=before_aircraft - 1,
            ),
            method="q2_local_exact_master",
        )
    except RuntimeError:
        return Q2LocalRepair(
            None,
            {
                **base_diagnostics,
                "repair_success": False,
                "reason": "no_milp_incumbent",
                "runtime_seconds": round(time.perf_counter() - started, 6),
            },
        )
    master = local_solution.diagnostics["q2_master"]
    combined = _combine_solution(
        current,
        local_solution,
        frozenset(destroyed),
        data,
        diagnostics={"local_master": master},
    )
    selected_columns = [
        {
            "base_airport": route.base_airport,
            "aircraft_type": route.aircraft_type,
            "service_order": list(route.service_facilities),
        }
        for route in local_solution.routes
    ]
    return Q2LocalRepair(
        combined,
        {
            **base_diagnostics,
            "repair_success": True,
            "after_routes": len(local_solution.routes),
            "after_aircraft_minutes": local_solution.metrics.total_aircraft_time_minutes,
            "after_passenger_minutes": local_solution.metrics.total_passenger_travel_time_minutes,
            "route_ejected": len(local_solution.routes) < len(affected_routes),
            "compatible_assignments": master["compatible_assignments"],
            "candidate_pool_hash": master["candidate_pool_hash"],
            "primary_status": master["primary_status"],
            "primary_dual_bound": master["primary_dual_bound"],
            "primary_mip_gap": master["primary_mip_gap"],
            "selected_columns": selected_columns,
            "runtime_seconds": round(time.perf_counter() - started, 6),
            "evaluator_calls": len(local_solution.routes) + len(current.routes),
        },
    )


def solve_q2_lns(
    initial: Solution,
    data: ProblemData,
    *,
    config: Q2LnsConfig | None = None,
    cache: SolverCache | None = None,
) -> Q2LnsResult:
    """Strict-improvement LNS with exact local MILP repacking."""
    config = config or Q2LnsConfig()
    cache = cache or SolverCache(data)
    started = time.perf_counter()
    current = initial
    logs: list[dict[str, object]] = []
    stats: dict[str, dict[str, object]] = {
        operator: {
            "operator": operator,
            "uses": 0,
            "repair_success": 0,
            "accepted": 0,
            "primary_improvement": 0,
            "new_best": 0,
            "primary_gain_minutes": 0,
            "runtime_seconds": 0.0,
            "local_master_sizes": [],
        }
        for operator in config.operators
    }
    first_improvement_seconds: float | None = None
    time_to_best_seconds: float | None = None
    for iteration in range(config.iterations):
        operator = config.operators[iteration % len(config.operators)]
        neighborhood = select_q2_neighborhood(
            current,
            data,
            operator=operator,
            iteration=iteration,
            config=config,
        )
        before = current.metrics
        repair = exact_q2_local_repair(
            current,
            data,
            neighborhood,
            cache=cache,
            config=config,
        )
        candidate = repair.solution
        accepted = bool(
            candidate is not None
            and candidate.metrics.comparison_key() < current.metrics.comparison_key()
        )
        primary_gain = (
            before.total_aircraft_time_minutes
            - candidate.metrics.total_aircraft_time_minutes
            if candidate is not None
            else 0
        )
        secondary_gain = (
            before.total_passenger_travel_time_minutes
            - candidate.metrics.total_passenger_travel_time_minutes
            if candidate is not None
            and candidate.metrics.total_aircraft_time_minutes
            == before.total_aircraft_time_minutes
            else 0
        )
        elapsed = time.perf_counter() - started
        if accepted:
            current = candidate
            if first_improvement_seconds is None:
                first_improvement_seconds = elapsed
            time_to_best_seconds = elapsed
        op = stats[operator]
        op["uses"] = int(op["uses"]) + 1
        op["repair_success"] = int(op["repair_success"]) + int(candidate is not None)
        op["accepted"] = int(op["accepted"]) + int(accepted)
        op["primary_improvement"] = int(op["primary_improvement"]) + int(
            accepted and primary_gain > 0
        )
        op["new_best"] = int(op["new_best"]) + int(accepted)
        op["primary_gain_minutes"] = int(op["primary_gain_minutes"]) + max(
            0, primary_gain if accepted else 0
        )
        op["runtime_seconds"] = float(op["runtime_seconds"]) + float(
            repair.diagnostics.get("runtime_seconds", 0.0)
        )
        sizes = op["local_master_sizes"]
        assert isinstance(sizes, list)
        sizes.append(int(repair.diagnostics.get("compatible_assignments", 0)))
        logs.append(
            {
                "iteration": iteration,
                "current_objective": current.metrics.total_aircraft_time_minutes,
                "best_objective": current.metrics.total_aircraft_time_minutes,
                "destroy_operator": operator,
                "repair_policy": "geometry_exact_local_milp",
                "destroyed_routes": list(neighborhood),
                "affected_demand_groups": repair.diagnostics.get(
                    "affected_demand_groups", 0
                ),
                "facilities": repair.diagnostics.get("facilities", []),
                "candidate_sequences": repair.diagnostics.get("candidate_sequences", 0),
                "candidate_features": {"ranking": "raw_geometry"},
                "local_master_size": repair.diagnostics.get(
                    "compatible_assignments", 0
                ),
                "selected_columns": repair.diagnostics.get("selected_columns", []),
                "repair_success": repair.diagnostics.get("repair_success", False),
                "accepted": accepted,
                "primary_gain": primary_gain if accepted else 0,
                "secondary_gain": secondary_gain if accepted else 0,
                "route_ejected": bool(repair.diagnostics.get("route_ejected", False)),
                "runtime": repair.diagnostics.get("runtime_seconds", 0.0),
                "evaluator_calls": repair.diagnostics.get("evaluator_calls", 0),
                "primary_status": repair.diagnostics.get("primary_status"),
                "restricted_dual_bound": repair.diagnostics.get("primary_dual_bound"),
                "restricted_gap": repair.diagnostics.get("primary_mip_gap"),
                "bound_scope": "restricted_local_master",
            }
        )

    operator_rows: list[dict[str, object]] = []
    for operator in config.operators:
        values = stats[operator]
        sizes = values.pop("local_master_sizes")
        assert isinstance(sizes, list)
        runtime = float(values["runtime_seconds"])
        operator_rows.append(
            {
                **values,
                "runtime_seconds": round(runtime, 6),
                "mean_local_master_size": round(mean(sizes), 3) if sizes else 0.0,
                "max_local_master_size": max(sizes, default=0),
            }
        )
    elapsed = time.perf_counter() - started
    final = replace(
        current,
        diagnostics={
            **current.diagnostics,
            "q2_lns": {
                "config": {
                    "iterations": config.iterations,
                    "neighborhood_size": config.neighborhood_size,
                    "max_sequence_length": config.max_sequence_length,
                    "candidate_sequence_budget": config.candidate_sequence_budget,
                    "local_primary_seconds": config.local_primary_seconds,
                    "local_secondary_seconds": config.local_secondary_seconds,
                    "seed": config.seed,
                    "operators": list(config.operators),
                },
                "initial_metrics": initial.metrics.to_dict(),
                "final_metrics": current.metrics.to_dict(),
                "first_improvement_seconds": first_improvement_seconds,
                "time_to_best_seconds": time_to_best_seconds,
                "operator_stats": operator_rows,
                "cache": cache.stats(),
                "elapsed_seconds": round(elapsed, 6),
            },
        },
    )
    return Q2LnsResult(final, tuple(logs), tuple(operator_rows), elapsed)
