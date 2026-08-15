from __future__ import annotations

import math
import random
import time
import hashlib
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
from .q2_flow import (
    Q2DirectedFlowGraph,
    build_q2_directed_flow_graph,
    flow_aware_local_sequences,
    q2_sequence_features,
)
from .q2_learning import classify_q2_candidate_event


DESTROY_OPERATORS = (
    "high_cost_route",
    "low_utilization_route",
    "shared_facility_flow",
    "land_heavy_route",
    "ejection_chain",
    "flight_elimination",
    "fix_and_optimize",
    "cross_exchange",
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
    candidate_policy: str = "geometry"
    operator_selection: str = "round_robin"
    adaptive_reaction: float = 0.2
    operators: tuple[str, ...] = DESTROY_OPERATORS[:4]
    max_wall_seconds: float | None = None
    destroy_size_policy: str = "fixed"
    adaptive_destroy_sizes: tuple[int, ...] = (2, 3, 4)
    medium_stagnation: int = 3
    large_stagnation: int = 6
    large_neighborhood_frequency: int = 4
    acceptance_policy: str = "strict"
    sa_initial_temperature: float = 12.0
    sa_cooling_rate: float = 0.92
    sa_min_temperature: float = 0.5
    targeted_four_stop: bool = False
    targeted_five_stop: bool = False
    five_stop_min_stagnation: int = 6
    five_stop_frequency: int = 5
    portfolio_geometry_slots: int = 14
    portfolio_context_slots: int = 6
    exploration_slots: int = 0
    run_purpose: str = "optimization"
    candidate_logging: bool = True

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
        if self.candidate_policy not in {
            "geometry", "flow", "context", "portfolio", "enrichment"
        }:
            raise ValueError(
                "candidate_policy must be geometry, flow, context, portfolio, or enrichment"
            )
        if self.operator_selection not in {"round_robin", "adaptive_roulette"}:
            raise ValueError("operator_selection must be round_robin or adaptive_roulette")
        if not 0.0 < self.adaptive_reaction <= 1.0:
            raise ValueError("adaptive_reaction must be in (0, 1]")
        if self.max_wall_seconds is not None and self.max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be positive when provided")
        if self.destroy_size_policy not in {"fixed", "adaptive"}:
            raise ValueError("destroy_size_policy must be fixed or adaptive")
        if not self.adaptive_destroy_sizes or min(self.adaptive_destroy_sizes) < 2:
            raise ValueError("adaptive_destroy_sizes must contain route counts >= 2")
        if tuple(sorted(set(self.adaptive_destroy_sizes))) != self.adaptive_destroy_sizes:
            raise ValueError("adaptive_destroy_sizes must be sorted and unique")
        if self.medium_stagnation < 1 or self.large_stagnation < self.medium_stagnation:
            raise ValueError("adaptive stagnation thresholds are invalid")
        if self.large_neighborhood_frequency < 1:
            raise ValueError("large_neighborhood_frequency must be positive")
        if self.acceptance_policy not in {"strict", "sa"}:
            raise ValueError("acceptance_policy must be strict or sa")
        if self.sa_initial_temperature <= 0 or self.sa_min_temperature <= 0:
            raise ValueError("SA temperatures must be positive")
        if not 0.0 < self.sa_cooling_rate < 1.0:
            raise ValueError("sa_cooling_rate must be in (0, 1)")
        if self.five_stop_min_stagnation < 1 or self.five_stop_frequency < 1:
            raise ValueError("targeted five-stop trigger values must be positive")
        if min(
            self.portfolio_geometry_slots,
            self.portfolio_context_slots,
            self.exploration_slots,
        ) < 0:
            raise ValueError("portfolio slot counts must be nonnegative")
        if self.run_purpose not in {"optimization", "ml_logging"}:
            raise ValueError("run_purpose must be optimization or ml_logging")


@dataclass(frozen=True)
class Q2LocalRepair:
    solution: Solution | None
    diagnostics: dict[str, object]


@dataclass(frozen=True)
class Q2LnsResult:
    solution: Solution
    iteration_log: tuple[dict[str, object], ...]
    candidate_log: tuple[dict[str, object], ...]
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


def _route_residual_capacity(route: RoutePlan, data: ProblemData) -> int:
    evaluation = _route_evaluation(route, data)
    capacity = data.config.aircraft_types[route.aircraft_type].seats
    peak = max((leg.departure_load for leg in evaluation.legs), default=0)
    return capacity - peak


def _ejection_potential_key(
    solution: Solution,
    data: ProblemData,
    index: int,
) -> tuple[float, ...]:
    """Cheap, deterministic route-level priority for exact chain repacking.

    The tuple deliberately uses ranks/ordering rather than a brittle weighted
    surrogate.  A promising source is lightly loaded, overlaps other routes,
    carries flexible LAND demand and is expensive relative to its passenger
    count.  Exact local repair remains the feasibility and value decision.
    """
    route = solution.routes[index]
    evaluation = _route_evaluation(route, data)
    facilities = _route_facilities(route, data)
    overlap = sum(
        len(facilities & _route_facilities(other, data))
        for other_index, other in enumerate(solution.routes)
        if other_index != index
    )
    passengers = max(1, route.passenger_count)
    return (
        float(route.passenger_count),
        evaluation.seat_utilization,
        -float(overlap),
        -_land_share(route),
        -float(evaluation.total_aircraft_time_minutes / passengers),
        float(index),
    )


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
    elif operator in {"ejection_chain", "flight_elimination"}:
        key = lambda index: _ejection_potential_key(solution, data, index)
    elif operator == "fix_and_optimize":
        frequency = Counter(
            facility
            for route in solution.routes
            for facility in _route_facilities(route, data)
        )
        key = lambda index: (
            -evaluations[index].total_aircraft_time_minutes,
            evaluations[index].seat_utilization,
            -sum(frequency[node] for node in _route_facilities(solution.routes[index], data)),
            index,
        )
    elif operator == "cross_exchange":
        key = lambda index: (
            -len(_route_facilities(solution.routes[index], data)),
            evaluations[index].seat_utilization,
            -_land_share(solution.routes[index]),
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


def _ejection_chain_targets(
    solution: Solution,
    data: ProblemData,
    source_index: int,
    *,
    count: int,
) -> list[int]:
    """Build a route-level repacking chain A -> B -> C deterministically."""
    if count <= 0:
        return []
    selected: list[int] = []
    remaining = set(range(len(solution.routes))) - {source_index}
    anchor_facilities = set(_route_facilities(solution.routes[source_index], data))
    source_airport = solution.routes[source_index].base_airport
    while remaining and len(selected) < count:
        def key(index: int) -> tuple[float, ...]:
            route = solution.routes[index]
            facilities = _route_facilities(route, data)
            return (
                -float(len(anchor_facilities & facilities)),
                -float(_flow_between(frozenset(anchor_facilities), facilities, data)),
                -float(route.base_airport == source_airport),
                -float(_route_residual_capacity(route, data)),
                _route_evaluation(route, data).seat_utilization,
                float(index),
            )

        chosen = min(remaining, key=key)
        selected.append(chosen)
        remaining.remove(chosen)
        anchor_facilities.update(_route_facilities(solution.routes[chosen], data))
    return selected


def adaptive_q2_destroy_size(
    config: Q2LnsConfig,
    *,
    iteration: int,
    stagnation: int,
    recent_success_rate: float,
    recent_mean_runtime: float,
) -> int:
    """Classical, interpretable destroy-scale controller.

    Fixed mode reproduces the Standard ALNS control exactly.  Adaptive mode
    normally uses the smallest neighborhood, moves to the middle size after
    stagnation, and only attempts the largest size periodically.  Expensive
    recent repairs or a fresh improvement pull the scale back down.
    """
    if config.destroy_size_policy == "fixed":
        return config.neighborhood_size
    sizes = config.adaptive_destroy_sizes
    small = sizes[0]
    medium = sizes[min(1, len(sizes) - 1)]
    large = sizes[-1]
    if stagnation == 0:
        return small
    if recent_mean_runtime > 1.25 * config.local_primary_seconds:
        return small
    if (
        stagnation >= config.large_stagnation
        and iteration % config.large_neighborhood_frequency == 0
    ):
        return large
    if stagnation >= config.medium_stagnation or recent_success_rate < 0.2:
        return medium
    return small


def select_q2_neighborhood(
    solution: Solution,
    data: ProblemData,
    *,
    operator: str,
    iteration: int,
    config: Q2LnsConfig,
    neighborhood_size: int | None = None,
) -> tuple[int, ...]:
    """Select one source route and related targets reproducibly for a seed."""
    sources = _source_order(solution, data, operator)[: config.source_pool_size]
    source_slot = (iteration // len(config.operators) + config.seed) % len(sources)
    source = sources[source_slot]
    size = neighborhood_size or config.neighborhood_size
    if operator in {"ejection_chain", "flight_elimination"}:
        targets = _ejection_chain_targets(
            solution,
            data,
            source,
            count=min(config.target_pool_size, size - 1),
        )
    else:
        targets = _target_order(solution, data, source)[: config.target_pool_size]
    rng = random.Random((config.seed + 1) * 1_000_003 + iteration * 97)
    # Preserve the strongest related target and diversify the remaining slots.
    selected = targets[:1]
    tail = targets[1:]
    rng.shuffle(tail)
    selected.extend(tail[: max(0, size - 2)])
    if len(selected) < size - 1:
        selected.extend(
            target for target in targets if target not in selected
        )
    return tuple([source, *selected[: size - 1]])


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


def _local_sequence_universe(
    data: ProblemData,
    routes: Sequence[RoutePlan],
    *,
    max_sequence_length: int,
) -> tuple[
    set[tuple[str, ...]],
    set[tuple[str, ...]],
    set[tuple[str, ...]],
]:
    facilities = sorted(
        {node for route in routes for node in _route_facilities(route, data)}
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
    generated = {
        sequence
        for length in range(2, min(max_sequence_length, len(facilities)) + 1)
        for sequence in permutations(facilities, length)
        if _sequence_supports_local_demand(sequence, data)
    }
    return required, base, generated


def _sequence_local_demand(sequence: tuple[str, ...], data: ProblemData) -> int:
    positions = {node: index for index, node in enumerate(sequence)}
    airports = set(data.config.airports)
    supported = 0
    for (origin, destination), pool in data.q2_pools.items():
        origin_ok = origin == "LAND" or origin in airports or origin in positions
        destination_ok = destination == "LAND" or destination in airports or destination in positions
        if not origin_ok or not destination_ok:
            continue
        if origin in positions and destination in positions and positions[origin] >= positions[destination]:
            continue
        supported += pool.quantity
    return supported


def _rank_percentiles(
    values: dict[tuple[str, ...], float],
    *,
    higher_is_better: bool,
) -> dict[tuple[str, ...], float]:
    ordered = sorted(
        values,
        key=lambda item: (
            -values[item] if higher_is_better else values[item],
            len(item),
            item,
        ),
    )
    denominator = max(1, len(ordered) - 1)
    return {sequence: 1.0 - rank / denominator for rank, sequence in enumerate(ordered)}


def rank_q2_local_sequences(
    data: ProblemData,
    routes: Sequence[RoutePlan],
    *,
    max_sequence_length: int,
    budget: int,
    policy: str,
    flow_graph: Q2DirectedFlowGraph | None = None,
    prioritize_four_stop: bool = False,
    portfolio_geometry_slots: int = 14,
    portfolio_context_slots: int = 6,
    exploration_slots: int = 0,
    selection_seed: int = 0,
) -> tuple[
    tuple[tuple[str, ...], ...],
    dict[tuple[str, ...], dict[str, object]],
    list[dict[str, object]],
]:
    """Rank the complete bounded local sequence universe and retain Top-K.

    Context ranking uses equal-weight percentile components.  This keeps the
    first version interpretable and makes every unselected sequence an
    explicit censored observation rather than a false negative.
    """
    required, base, generated = _local_sequence_universe(
        data,
        routes,
        max_sequence_length=max_sequence_length,
    )
    incumbent = required | base
    exploratory = sorted(generated - incumbent, key=lambda item: (len(item), item))
    feature_rows: dict[tuple[str, ...], dict[str, object]] = {}
    geometry_values = {
        sequence: float(_geometry_score(sequence, data)[0]) for sequence in exploratory
    }
    geometry_rank = _rank_percentiles(geometry_values, higher_is_better=False)
    context_rank: dict[tuple[str, ...], float] = {}
    context_ranked: list[tuple[str, ...]] = []
    if policy in {"context", "portfolio"}:
        if flow_graph is None:
            raise ValueError("context candidate policy requires a directed flow graph")
        raw_flow = {
            sequence: q2_sequence_features(sequence, data, flow_graph)
            for sequence in exploratory
        }
        demand_values = {
            sequence: float(_sequence_local_demand(sequence, data))
            for sequence in exploratory
        }
        coverage_values = {
            sequence: sum(
                len(set(sequence) & set(route.service_facilities))
                / max(1, len(set(route.service_facilities)))
                for route in routes
            )
            for sequence in exploratory
        }
        capacity_values = {
            sequence: raw_flow[sequence].capacity_fit for sequence in exploratory
        }
        flow_values = {
            sequence: float(
                raw_flow[sequence].directed_shuttle_flow
                + raw_flow[sequence].flow_complementarity
            )
            for sequence in exploratory
        }
        airport_values = {
            sequence: float(raw_flow[sequence].fixed_airport_affinity)
            for sequence in exploratory
        }
        component_ranks = {
            "geometry": geometry_rank,
            "capacity": _rank_percentiles(capacity_values, higher_is_better=True),
            "ejection_coverage": _rank_percentiles(coverage_values, higher_is_better=True),
            "local_demand": _rank_percentiles(demand_values, higher_is_better=True),
            "flow_context": _rank_percentiles(flow_values, higher_is_better=True),
            "airport": _rank_percentiles(airport_values, higher_is_better=True),
        }
        for sequence in exploratory:
            components = {
                name: round(values[sequence], 6)
                for name, values in component_ranks.items()
            }
            context_score = mean(components.values())
            feature_rows[sequence] = {
                **raw_flow[sequence].to_dict(),
                "local_supported_demand": int(demand_values[sequence]),
                "ejection_coverage": round(coverage_values[sequence], 6),
                "context_components": components,
                "context_score": round(context_score, 6),
            }
        ranked = sorted(
            exploratory,
            key=lambda item: (-float(feature_rows[item]["context_score"]), len(item), item),
        )
        context_ranked = ranked
        context_rank = _rank_percentiles(
            {sequence: float(feature_rows[sequence]["context_score"]) for sequence in exploratory},
            higher_is_better=True,
        )
    else:
        for sequence in exploratory:
            feature_rows[sequence] = {
                "route_distance_km": round(geometry_values[sequence], 6),
                "geometry_percentile": round(geometry_rank[sequence], 6),
            }
        ranked = sorted(exploratory, key=lambda item: _geometry_score(item, data))

    room = max(0, budget - len(incumbent))
    selected_exploratory: list[tuple[str, ...]] = []
    portfolio_source: dict[tuple[str, ...], str] = {}
    if policy == "portfolio":
        geometry_ranked = sorted(exploratory, key=lambda item: _geometry_score(item, data))
        exploration_reserve = min(exploration_slots, room)
        exploit_limit = max(0, room - exploration_reserve)
        for sequence in geometry_ranked[: min(exploit_limit, portfolio_geometry_slots)]:
            selected_exploratory.append(sequence)
            portfolio_source[sequence] = "geometry"
        for sequence in context_ranked:
            if len(selected_exploratory) >= min(
                exploit_limit, portfolio_geometry_slots + portfolio_context_slots
            ):
                break
            if sequence not in selected_exploratory:
                selected_exploratory.append(sequence)
                portfolio_source[sequence] = "context"
        remaining = [
            sequence for sequence in exploratory if sequence not in selected_exploratory
        ]
        random.Random(selection_seed).shuffle(remaining)
        for sequence in remaining[: min(exploration_reserve, max(0, room - len(selected_exploratory)))]:
            selected_exploratory.append(sequence)
            portfolio_source[sequence] = "exploration"
        for sequence in geometry_ranked:
            if len(selected_exploratory) >= room:
                break
            if sequence not in selected_exploratory:
                selected_exploratory.append(sequence)
                portfolio_source[sequence] = "geometry_fill"
    if prioritize_four_stop and room and policy != "portfolio":
        reserved = min(max(1, room // 4), 4)
        selected_exploratory.extend(
            sequence for sequence in ranked if len(sequence) == 4
        )
        selected_exploratory = selected_exploratory[:reserved]
    if policy != "portfolio":
        selected_exploratory.extend(
            sequence for sequence in ranked if sequence not in selected_exploratory
        )
    selected_exploratory = selected_exploratory[:room]
    selected = incumbent | set(selected_exploratory)
    rank_lookup = {sequence: rank + 1 for rank, sequence in enumerate(ranked)}
    candidate_rows = []
    for sequence in sorted(incumbent | set(exploratory), key=lambda item: (len(item), item)):
        chosen = sequence in selected
        candidate_rows.append(
            {
                "candidate_sequence": list(sequence),
                "candidate_variant": None,
                "airport": None,
                "aircraft_type": None,
                "features": feature_rows.get(sequence, {"incumbent_sequence": True}),
                "rank_before_exact": rank_lookup.get(sequence, 0),
                "rank_score_geometry": round(geometry_rank.get(sequence, 0.0), 6),
                "rank_score_context": round(context_rank.get(sequence, 0.0), 6),
                "portfolio_source": (
                    "incumbent" if sequence in incumbent else portfolio_source.get(
                        sequence, policy
                    )
                ),
                "stage_generated": True,
                "stage_ranked": sequence not in incumbent,
                "passed_cheap_filter": True,
                "top_k_selected": chosen,
                "selected_for_exact": chosen,
                "exact_variant_generated": False,
                "entered_local_master": False,
                "milp_candidate": False,
                "milp_selected": False,
                "evaluation_state": "pending_exact" if chosen else "not_evaluated",
                "label_censored": not chosen,
            }
        )
    return (
        tuple(sorted(selected, key=lambda item: (len(item), item))),
        feature_rows,
        candidate_rows,
    )


def geometry_local_sequences(
    data: ProblemData,
    routes: Sequence[RoutePlan],
    *,
    max_sequence_length: int,
    budget: int,
) -> tuple[tuple[str, ...], ...]:
    """Bounded raw/geometry candidate control for local exact repair."""
    sequences, _, _ = rank_q2_local_sequences(
        data,
        routes,
        max_sequence_length=max_sequence_length,
        budget=budget,
        policy="geometry",
    )
    return sequences


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
    flow_graph: Q2DirectedFlowGraph | None = None,
    require_primary_improvement: bool = True,
    allowed_primary_deterioration_minutes: int = 0,
    prioritize_four_stop: bool = False,
    candidate_seed_routes: Sequence[RoutePlan] = (),
    selection_seed: int = 0,
    search_context: dict[str, object] | None = None,
) -> Q2LocalRepair:
    started = time.perf_counter()
    destroyed = tuple(sorted(set(route_indices)))
    if len(destroyed) < 2 or destroyed[-1] >= len(current.routes):
        raise ValueError("Invalid Q2 local-repair route indices")
    affected_routes = tuple(current.routes[index] for index in destroyed)
    ranking_routes = (*affected_routes, *candidate_seed_routes)
    local_data = build_q2_local_data(data, affected_routes)
    sequence_features: dict[tuple[str, ...], object] = {}
    candidate_rows: list[dict[str, object]] = []
    if config.candidate_policy == "flow":
        if flow_graph is None:
            raise ValueError("flow candidate policy requires a directed flow graph")
        sequences, raw_features = flow_aware_local_sequences(
            local_data,
            ranking_routes,
            flow_graph,
            max_sequence_length=config.max_sequence_length,
            budget=config.candidate_sequence_budget,
        )
        sequence_features = {
            sequence: feature.to_dict() for sequence, feature in raw_features.items()
        }
    else:
        sequences, sequence_features, candidate_rows = rank_q2_local_sequences(
            local_data,
            ranking_routes,
            max_sequence_length=config.max_sequence_length,
            budget=config.candidate_sequence_budget,
            policy=config.candidate_policy,
            flow_graph=flow_graph,
            prioritize_four_stop=prioritize_four_stop,
            portfolio_geometry_slots=config.portfolio_geometry_slots,
            portfolio_context_slots=config.portfolio_context_slots,
            exploration_slots=config.exploration_slots,
            selection_seed=selection_seed,
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
    route_context = {
        "current_duration_minutes": before_aircraft,
        "current_route_count": len(affected_routes),
        "current_route_passengers": sum(route.passenger_count for route in affected_routes),
        "current_mean_utilization": round(
            mean(item.seat_utilization for item in before_evaluations), 6
        ),
        "current_mean_stop_count": round(
            mean(len(route.service_facilities) for route in affected_routes), 6
        ),
        "current_min_residual_slack": min(
            _route_residual_capacity(route, data) for route in affected_routes
        ),
        "current_mean_residual_slack": round(
            mean(_route_residual_capacity(route, data) for route in affected_routes), 6
        ),
        "current_land_fraction": round(
            mean(_land_share(route) for route in affected_routes), 6
        ),
        "current_aircraft_types": sorted({route.aircraft_type for route in affected_routes}),
        "current_base_airports": sorted({route.base_airport for route in affected_routes}),
    }
    for row in candidate_rows:
        sequence = tuple(row["candidate_sequence"])
        features = dict(row.get("features", {}))
        if flow_graph is not None and sequence:
            features.update(q2_sequence_features(sequence, local_data, flow_graph).to_dict())
        pairwise = [
            data.matrix[left][right]
            for left_index, left in enumerate(sequence)
            for right in sequence[left_index + 1 :]
        ]
        airport_profile = [
            min(data.matrix[airport][node] for airport in data.config.airports)
            for node in sequence
        ]
        features.update(
            {
                "service_node_count": len(sequence),
                "min_pairwise_distance_km": min(pairwise, default=0.0),
                "max_pairwise_distance_km": max(pairwise, default=0.0),
                "min_airport_distance_km": min(airport_profile, default=0.0),
                "max_airport_distance_km": max(airport_profile, default=0.0),
                **route_context,
            }
        )
        row["features"] = features
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
    if candidate_rows:
        selected_sequences = {
            tuple(row["candidate_sequence"])
            for row in candidate_rows
            if row["top_k_selected"]
        }
        expanded_rows: list[dict[str, object]] = [
            row for row in candidate_rows if not row["top_k_selected"]
        ]
        variants_by_sequence: dict[tuple[str, ...], list[object]] = defaultdict(list)
        for variant in variants:
            variants_by_sequence[variant.service_order].append(variant)
        for sequence in sorted(selected_sequences, key=lambda item: (len(item), item)):
            matches = variants_by_sequence.get(sequence, [])
            template = next(
                row
                for row in candidate_rows
                if tuple(row["candidate_sequence"]) == sequence
            )
            if not matches:
                expanded_rows.append(
                    {
                        **template,
                        "exact_variant_generated": False,
                        "evaluation_state": "exact_evaluated",
                        "label_censored": False,
                    }
                )
                continue
            for variant in matches:
                expanded_rows.append(
                    {
                        **template,
                        "candidate_variant": [
                            [stop.facility_id, int(stop.refuel)]
                            for stop in variant.route.stops
                        ],
                        "airport": variant.base_airport,
                        "aircraft_type": variant.aircraft_type,
                        "technical_stop_augmentation_count": sum(
                            int(not stop.is_service) for stop in variant.route.stops[1:-1]
                        ),
                        "refuel_involvement": any(
                            stop.refuel for stop in variant.route.stops
                        ),
                        "exact_variant_generated": True,
                        "milp_candidate": True,
                        "entered_local_master": True,
                        "evaluation_state": "exact_evaluated",
                        "label_censored": False,
                    }
                )
        candidate_rows = expanded_rows
    for row in candidate_rows:
        row["search_context"] = search_context or {}
    if not variants:
        return Q2LocalRepair(
            None,
            {
                **base_diagnostics,
                "repair_success": False,
                "reason": "no_variants",
                "candidate_log": candidate_rows,
            },
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
                primary_upper_bound_minutes=(
                    before_aircraft - 1
                    if require_primary_improvement
                    else before_aircraft + max(0, allowed_primary_deterioration_minutes)
                ),
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
                "candidate_log": candidate_rows,
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
    existing_orders = {tuple(route.service_facilities) for route in affected_routes}
    selected_columns = [
        {
            "base_airport": route.base_airport,
            "aircraft_type": route.aircraft_type,
            "service_order": list(route.service_facilities),
            "new_candidate": tuple(route.service_facilities) not in existing_orders,
            "candidate_features": sequence_features.get(tuple(route.service_facilities)),
        }
        for route in local_solution.routes
    ]
    selected_keys = {
        (
            route.base_airport,
            route.aircraft_type,
            tuple(route.service_facilities),
        )
        for route in local_solution.routes
    }
    for row in candidate_rows:
        row["milp_selected"] = (
            row.get("airport"),
            row.get("aircraft_type"),
            tuple(row["candidate_sequence"]),
        ) in selected_keys
        row["repair_feasible"] = True
        row["search_context"] = search_context or {}
        row["evaluation_cost_ms"] = round(
            1000.0 * (time.perf_counter() - started) / max(1, len(candidate_rows)), 6
        )
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
            "selected_new_candidates": sum(
                int(column["new_candidate"]) for column in selected_columns
            ),
            "selected_3_5_stop_candidates": sum(
                int(column["new_candidate"] and len(column["service_order"]) >= 3)
                for column in selected_columns
            ),
            "selected_4_stop_candidates": sum(
                int(column["new_candidate"] and len(column["service_order"]) == 4)
                for column in selected_columns
            ),
            "runtime_seconds": round(time.perf_counter() - started, 6),
            "evaluator_calls": len(local_solution.routes) + len(current.routes),
            "candidate_log": candidate_rows,
        },
    )


def _route_passengers(route: RoutePlan) -> frozenset[str]:
    return frozenset(assignment.person_id for assignment in route.assignments)


def _route_structure_similarity(left: RoutePlan, right: RoutePlan) -> float:
    left_people = _route_passengers(left)
    right_people = _route_passengers(right)
    people_union = left_people | right_people
    passenger_jaccard = (
        len(left_people & right_people) / len(people_union) if people_union else 1.0
    )
    left_facilities = set(left.service_facilities)
    right_facilities = set(right.service_facilities)
    facility_union = left_facilities | right_facilities
    facility_jaccard = (
        len(left_facilities & right_facilities) / len(facility_union)
        if facility_union
        else 1.0
    )
    metadata = mean(
        (
            float(left.base_airport == right.base_airport),
            float(left.aircraft_type == right.aircraft_type),
            float(tuple(left.service_facilities) == tuple(right.service_facilities)),
        )
    )
    return 0.6 * passenger_jaccard + 0.25 * facility_jaccard + 0.15 * metadata


def q2_solution_diversity(left: Solution, right: Solution) -> float:
    """Symmetric route-composition distance for a compact elite pool."""
    if not left.routes or not right.routes:
        return 1.0
    left_match = mean(
        max(_route_structure_similarity(route, other) for other in right.routes)
        for route in left.routes
    )
    right_match = mean(
        max(_route_structure_similarity(route, other) for other in left.routes)
        for route in right.routes
    )
    return round(1.0 - 0.5 * (left_match + right_match), 9)


def exact_q2_elite_recombination(
    current: Solution,
    partner: Solution,
    data: ProblemData,
    *,
    cache: SolverCache,
    config: Q2LnsConfig,
    iteration: int = 0,
) -> Q2LocalRepair:
    """Destroy one elite-difference region and repair with both route vocabularies."""
    source_order = sorted(
        range(len(current.routes)),
        key=lambda index: (
            max(
                _route_structure_similarity(current.routes[index], other)
                for other in partner.routes
            ),
            -_route_evaluation(current.routes[index], data).total_aircraft_time_minutes,
            index,
        ),
    )
    source = source_order[iteration % min(len(source_order), config.source_pool_size)]
    targets = _target_order(current, data, source)
    size = max(2, config.neighborhood_size)
    neighborhood = tuple([source, *targets[: size - 1]])
    affected_people = set().union(
        *(_route_passengers(current.routes[index]) for index in neighborhood)
    )
    affected_facilities = set().union(
        *(_route_facilities(current.routes[index], data) for index in neighborhood)
    )
    partner_ranked = sorted(
        partner.routes,
        key=lambda route: (
            -len(affected_people & _route_passengers(route)),
            -len(affected_facilities & _route_facilities(route, data)),
            tuple(route.service_facilities),
            route.base_airport,
            route.aircraft_type,
        ),
    )
    partner_seeds = tuple(partner_ranked[: max(size, 2 * size)])
    repair = exact_q2_local_repair(
        current,
        data,
        neighborhood,
        cache=cache,
        config=config,
        flow_graph=(
            build_q2_directed_flow_graph(data)
            if config.candidate_policy in {"context", "portfolio"}
            else None
        ),
        candidate_seed_routes=partner_seeds,
        prioritize_four_stop=config.targeted_four_stop,
    )
    return Q2LocalRepair(
        repair.solution,
        {
            **repair.diagnostics,
            "repair_policy": "elite_difference_exact_recombination",
            "elite_diversity": q2_solution_diversity(current, partner),
            "partner_seed_routes": len(partner_seeds),
            "elite_neighborhood": list(neighborhood),
        },
    )


def heuristic_q2_enrichment_repair(
    current: Solution,
    data: ProblemData,
    route_indices: Iterable[int],
    *,
    cache: SolverCache,
    config: Q2LnsConfig,
) -> Q2LocalRepair:
    """Add geometry-ranked columns in three bounded solve/enrich rounds.

    This is heuristic column enrichment, not reduced-cost pricing: a failed or
    weaker restricted local solve receives a larger prefix of the same ranked
    candidate universe.  Total candidate and MILP budgets match the static
    control.
    """
    started = time.perf_counter()
    final_budget = config.candidate_sequence_budget
    budgets = tuple(
        sorted(
            {
                max(1, math.ceil(final_budget / 3)),
                max(1, math.ceil(2 * final_budget / 3)),
                final_budget,
            }
        )
    )
    weights = [0.25, 0.25, 0.5] if len(budgets) == 3 else [1.0 / len(budgets)] * len(budgets)
    rounds: list[dict[str, object]] = []
    best: Q2LocalRepair | None = None
    for round_index, (budget, weight) in enumerate(zip(budgets, weights), start=1):
        round_config = replace(
            config,
            candidate_policy="geometry",
            candidate_sequence_budget=budget,
            local_primary_seconds=max(0.05, config.local_primary_seconds * weight),
            local_secondary_seconds=0.0,
        )
        repair = exact_q2_local_repair(
            current,
            data,
            route_indices,
            cache=cache,
            config=round_config,
        )
        rounds.append(
            {
                "round": round_index,
                "candidate_sequence_budget": budget,
                "candidate_sequences": repair.diagnostics.get("candidate_sequences", 0),
                "candidate_variants": repair.diagnostics.get("candidate_variants", 0),
                "candidate_variants": repair.diagnostics.get("candidate_variants", 0),
                "compatible_assignments": repair.diagnostics.get(
                    "compatible_assignments", 0
                ),
                "primary_time_limit_seconds": round_config.local_primary_seconds,
                "repair_success": repair.solution is not None,
                "candidate_aircraft_minutes": (
                    repair.solution.metrics.total_aircraft_time_minutes
                    if repair.solution is not None
                    else None
                ),
            }
        )
        if repair.solution is not None and (
            best is None
            or best.solution is None
            or repair.solution.metrics.comparison_key()
            < best.solution.metrics.comparison_key()
        ):
            best = repair
    if best is None:
        return Q2LocalRepair(
            None,
            {
                "destroyed_routes": list(sorted(set(route_indices))),
                "repair_success": False,
                "reason": "no_enrichment_incumbent",
                "enrichment_rounds": rounds,
                "candidate_sequences": max(
                    (int(row["candidate_sequences"]) for row in rounds), default=0
                ),
                "candidate_variants": max(
                    (int(row["candidate_variants"]) for row in rounds), default=0
                ),
                "compatible_assignments": max(
                    (int(row["compatible_assignments"]) for row in rounds), default=0
                ),
                "runtime_seconds": round(time.perf_counter() - started, 6),
            },
        )
    return Q2LocalRepair(
        best.solution,
        {
            **best.diagnostics,
            "repair_policy": "heuristic_iterative_column_enrichment",
            "enrichment_rounds": rounds,
            "runtime_seconds": round(time.perf_counter() - started, 6),
        },
    )


def solve_q2_lns(
    initial: Solution,
    data: ProblemData,
    *,
    config: Q2LnsConfig | None = None,
    cache: SolverCache | None = None,
) -> Q2LnsResult:
    """Adaptive ALNS with exact local MILP repacking and immutable best."""
    config = config or Q2LnsConfig()
    cache = cache or SolverCache(data)
    started = time.perf_counter()
    current = initial
    best = initial
    flow_graph = (
        build_q2_directed_flow_graph(data)
        if config.candidate_policy in {"flow", "context", "portfolio"}
        or config.candidate_logging
        else None
    )
    logs: list[dict[str, object]] = []
    candidate_logs: list[dict[str, object]] = []
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
    operator_weights = {operator: 1.0 for operator in config.operators}
    operator_rng = random.Random((config.seed + 1) * 9_999_991)
    acceptance_rng = random.Random((config.seed + 1) * 15_485_863)
    stagnation = 0
    temperature = config.sa_initial_temperature
    size_stats: dict[int, dict[str, float]] = defaultdict(
        lambda: {
            "uses": 0,
            "repair_success": 0,
            "accepted": 0,
            "primary_gain_minutes": 0,
            "new_best": 0,
            "runtime_seconds": 0.0,
            "local_master_size_sum": 0.0,
        }
    )
    accepted_deteriorating_moves = 0
    deteriorating_minutes = 0
    best_after_deterioration = 0
    for iteration in range(config.iterations):
        if (
            config.max_wall_seconds is not None
            and time.perf_counter() - started >= config.max_wall_seconds
        ):
            break
        if config.operator_selection == "adaptive_roulette":
            operator = operator_rng.choices(
                config.operators,
                weights=[operator_weights[value] for value in config.operators],
                k=1,
            )[0]
        else:
            operator = config.operators[iteration % len(config.operators)]
        weight_before = operator_weights[operator]
        recent = logs[-5:]
        recent_success_rate = (
            mean(float(bool(row["repair_success"])) for row in recent)
            if recent
            else 1.0
        )
        recent_mean_runtime = (
            mean(float(row["runtime"]) for row in recent) if recent else 0.0
        )
        destroy_size = adaptive_q2_destroy_size(
            config,
            iteration=iteration,
            stagnation=stagnation,
            recent_success_rate=recent_success_rate,
            recent_mean_runtime=recent_mean_runtime,
        )
        trigger_reason: str | None = None
        if (
            config.targeted_five_stop
            and config.max_sequence_length >= 5
            and stagnation >= config.five_stop_min_stagnation
            and iteration % config.five_stop_frequency == 0
        ):
            destroy_size = max(destroy_size, 5)
            trigger_reason = "long_stagnation"
        if operator == "flight_elimination":
            destroy_size = max(destroy_size, 5)
            trigger_reason = "high_flight_elimination_potential"
        neighborhood = select_q2_neighborhood(
            current,
            data,
            operator=operator,
            iteration=iteration,
            config=config,
            neighborhood_size=destroy_size,
        )
        before = current.metrics
        if config.candidate_policy == "enrichment":
            repair = heuristic_q2_enrichment_repair(
                current,
                data,
                neighborhood,
                cache=cache,
                config=config,
            )
        else:
            repair = exact_q2_local_repair(
                current,
                data,
                neighborhood,
                cache=cache,
                config=config,
                flow_graph=flow_graph,
                require_primary_improvement=config.acceptance_policy == "strict",
                allowed_primary_deterioration_minutes=math.ceil(temperature),
                prioritize_four_stop=(
                    config.targeted_four_stop
                    and (destroy_size >= 4 or operator == "ejection_chain")
                ),
                selection_seed=(config.seed + 1) * 1_000_003 + iteration * 97,
                search_context={
                    "iteration": iteration,
                    "destroy_operator": operator,
                    "destroy_size": destroy_size,
                    "sa_temperature": round(temperature, 6),
                    "current_objective": current.metrics.total_aircraft_time_minutes,
                    "best_objective": best.metrics.total_aircraft_time_minutes,
                    "stagnation_length": stagnation,
                    "elite_restart_status": False,
                    "run_purpose": config.run_purpose,
                    "targeted_trigger": trigger_reason,
                },
            )
        candidate = repair.solution
        temperature_before = temperature
        primary_gain = (
            before.total_aircraft_time_minutes
            - candidate.metrics.total_aircraft_time_minutes
            if candidate is not None
            else 0
        )
        strict_accept = bool(
            candidate is not None
            and candidate.metrics.comparison_key() < current.metrics.comparison_key()
        )
        deterioration = max(0, -primary_gain)
        if candidate is not None and deterioration == 0 and not strict_accept:
            deterioration = max(
                0,
                math.ceil(
                    (
                        candidate.metrics.total_passenger_travel_time_minutes
                        - before.total_passenger_travel_time_minutes
                    )
                    / 1000.0
                ),
            )
        sa_probability = (
            math.exp(-max(1, deterioration) / max(temperature, config.sa_min_temperature))
            if candidate is not None and not strict_accept
            else 1.0 if strict_accept else 0.0
        )
        accepted = bool(
            strict_accept
            or (
                config.acceptance_policy == "sa"
                and candidate is not None
                and acceptance_rng.random() < sa_probability
            )
        )
        new_best = bool(
            accepted
            and candidate is not None
            and candidate.metrics.comparison_key() < best.metrics.comparison_key()
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
        if new_best:
            best = candidate
            time_to_best_seconds = elapsed
            if accepted_deteriorating_moves:
                best_after_deterioration += 1
            stagnation = 0
        else:
            stagnation += 1
        if accepted and not strict_accept:
            accepted_deteriorating_moves += 1
            deteriorating_minutes += max(0, -primary_gain)
        if config.acceptance_policy == "sa":
            temperature = max(
                config.sa_min_temperature,
                temperature * config.sa_cooling_rate,
            )
        if accepted and primary_gain > 0:
            reward = 6.0
        elif accepted:
            reward = 3.0
        elif candidate is not None:
            reward = 1.0
        else:
            reward = 0.2
        if config.operator_selection == "adaptive_roulette":
            reaction = config.adaptive_reaction
            operator_weights[operator] = (1.0 - reaction) * weight_before + reaction * reward
        op = stats[operator]
        op["uses"] = int(op["uses"]) + 1
        op["repair_success"] = int(op["repair_success"]) + int(candidate is not None)
        op["accepted"] = int(op["accepted"]) + int(accepted)
        op["primary_improvement"] = int(op["primary_improvement"]) + int(
            accepted and primary_gain > 0
        )
        op["new_best"] = int(op["new_best"]) + int(new_best)
        op["primary_gain_minutes"] = int(op["primary_gain_minutes"]) + max(
            0, primary_gain if accepted else 0
        )
        op["runtime_seconds"] = float(op["runtime_seconds"]) + float(
            repair.diagnostics.get("runtime_seconds", 0.0)
        )
        sizes = op["local_master_sizes"]
        assert isinstance(sizes, list)
        sizes.append(int(repair.diagnostics.get("compatible_assignments", 0)))
        size_row = size_stats[destroy_size]
        size_row["uses"] += 1
        size_row["repair_success"] += int(candidate is not None)
        size_row["accepted"] += int(accepted)
        size_row["primary_gain_minutes"] += max(0, primary_gain if accepted else 0)
        size_row["new_best"] += int(new_best)
        size_row["runtime_seconds"] += float(repair.diagnostics.get("runtime_seconds", 0.0))
        size_row["local_master_size_sum"] += int(
            repair.diagnostics.get("compatible_assignments", 0)
        )
        repair_candidate_rows = repair.diagnostics.get("candidate_log", [])
        if config.candidate_logging and isinstance(repair_candidate_rows, list):
            for row in repair_candidate_rows:
                sequence = tuple(row.get("candidate_sequence", ()))
                variant = row.get("candidate_variant")
                event_key = repr(
                    (
                        config.seed,
                        iteration,
                        operator,
                        neighborhood,
                        sequence,
                        variant,
                        row.get("airport"),
                        row.get("aircraft_type"),
                    )
                ).encode("utf-8")
                event = {
                        "candidate_id": hashlib.sha256(event_key).hexdigest()[:24],
                        "run_id": None,
                        "seed": config.seed,
                        "iteration": iteration,
                        "destroy_operator": operator,
                        "destroy_size": destroy_size,
                        "source_routes": list(neighborhood),
                        **row,
                        "repair_feasible": candidate is not None,
                        "repair_accepted": accepted,
                        "primary_gain": primary_gain if accepted else 0,
                        "secondary_gain": secondary_gain if accepted else 0,
                        "new_global_best": new_best,
                    }
                event["label_class"] = classify_q2_candidate_event(event)
                candidate_logs.append(event)
        logs.append(
            {
                "iteration": iteration,
                "current_objective": current.metrics.total_aircraft_time_minutes,
                "best_objective": best.metrics.total_aircraft_time_minutes,
                "destroy_operator": operator,
                "destroy_size": destroy_size,
                "targeted_trigger": trigger_reason,
                "stagnation_before": max(0, stagnation - (0 if new_best else 1)),
                "operator_weight_before": round(weight_before, 6),
                "operator_weight_after": round(operator_weights[operator], 6),
                "repair_policy": f"{config.candidate_policy}_exact_local_milp",
                "destroyed_routes": list(neighborhood),
                "affected_demand_groups": repair.diagnostics.get(
                    "affected_demand_groups", 0
                ),
                "facilities": repair.diagnostics.get("facilities", []),
                "candidate_sequences": repair.diagnostics.get("candidate_sequences", 0),
                "candidate_features": {"ranking": config.candidate_policy},
                "local_master_size": repair.diagnostics.get(
                    "compatible_assignments", 0
                ),
                "selected_columns": repair.diagnostics.get("selected_columns", []),
                "repair_success": repair.diagnostics.get("repair_success", False),
                "accepted": accepted,
                "new_best": new_best,
                "acceptance_policy": config.acceptance_policy,
                "temperature": round(temperature_before, 6),
                "sa_acceptance_probability": round(sa_probability, 9),
                "accepted_deteriorating": bool(accepted and not strict_accept),
                "primary_gain": primary_gain if accepted else 0,
                "secondary_gain": secondary_gain if accepted else 0,
                "route_ejected": bool(repair.diagnostics.get("route_ejected", False)),
                "flight_delta": (
                    int(repair.diagnostics.get("before_routes", 0))
                    - int(repair.diagnostics.get("after_routes", 0))
                    if repair.diagnostics.get("repair_success")
                    else 0
                ),
                "selected_new_candidates": repair.diagnostics.get(
                    "selected_new_candidates", 0
                ),
                "selected_3_5_stop_candidates": repair.diagnostics.get(
                    "selected_3_5_stop_candidates", 0
                ),
                "selected_4_stop_candidates": repair.diagnostics.get(
                    "selected_4_stop_candidates", 0
                ),
                "runtime": repair.diagnostics.get("runtime_seconds", 0.0),
                "evaluator_calls": repair.diagnostics.get("evaluator_calls", 0),
                "primary_status": repair.diagnostics.get("primary_status"),
                "restricted_dual_bound": repair.diagnostics.get("primary_dual_bound"),
                "restricted_gap": repair.diagnostics.get("primary_mip_gap"),
                "bound_scope": "restricted_local_master",
                "enrichment_rounds": repair.diagnostics.get("enrichment_rounds"),
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
    destroy_size_rows = []
    for size in sorted(size_stats):
        values = size_stats[size]
        uses = int(values["uses"])
        destroy_size_rows.append(
            {
                "destroy_size": size,
                "uses": uses,
                "repair_success": int(values["repair_success"]),
                "accepted": int(values["accepted"]),
                "primary_gain_minutes": int(values["primary_gain_minutes"]),
                "new_best": int(values["new_best"]),
                "runtime_seconds": round(values["runtime_seconds"], 6),
                "mean_local_master_size": round(
                    values["local_master_size_sum"] / uses, 3
                ) if uses else 0.0,
            }
        )
    final = replace(
        best,
        diagnostics={
            **best.diagnostics,
            "q2_lns": {
                "config": {
                    "iterations": config.iterations,
                    "max_wall_seconds": config.max_wall_seconds,
                    "neighborhood_size": config.neighborhood_size,
                    "destroy_size_policy": config.destroy_size_policy,
                    "adaptive_destroy_sizes": list(config.adaptive_destroy_sizes),
                    "medium_stagnation": config.medium_stagnation,
                    "large_stagnation": config.large_stagnation,
                    "large_neighborhood_frequency": config.large_neighborhood_frequency,
                    "max_sequence_length": config.max_sequence_length,
                    "candidate_sequence_budget": config.candidate_sequence_budget,
                    "local_primary_seconds": config.local_primary_seconds,
                    "local_secondary_seconds": config.local_secondary_seconds,
                    "seed": config.seed,
                    "candidate_policy": config.candidate_policy,
                    "operator_selection": config.operator_selection,
                    "adaptive_reaction": config.adaptive_reaction,
                    "acceptance_policy": config.acceptance_policy,
                    "sa_initial_temperature": config.sa_initial_temperature,
                    "sa_cooling_rate": config.sa_cooling_rate,
                    "sa_min_temperature": config.sa_min_temperature,
                    "targeted_four_stop": config.targeted_four_stop,
                    "targeted_five_stop": config.targeted_five_stop,
                    "five_stop_min_stagnation": config.five_stop_min_stagnation,
                    "five_stop_frequency": config.five_stop_frequency,
                    "portfolio_geometry_slots": config.portfolio_geometry_slots,
                    "portfolio_context_slots": config.portfolio_context_slots,
                    "exploration_slots": config.exploration_slots,
                    "run_purpose": config.run_purpose,
                    "candidate_logging": config.candidate_logging,
                    "operators": list(config.operators),
                },
                "initial_metrics": initial.metrics.to_dict(),
                "final_metrics": best.metrics.to_dict(),
                "terminal_current_metrics": current.metrics.to_dict(),
                "first_improvement_seconds": first_improvement_seconds,
                "time_to_best_seconds": time_to_best_seconds,
                "operator_stats": operator_rows,
                "destroy_size_stats": destroy_size_rows,
                "sa": {
                    "accepted_deteriorating_moves": accepted_deteriorating_moves,
                    "average_deterioration_minutes": round(
                        deteriorating_minutes / accepted_deteriorating_moves, 6
                    ) if accepted_deteriorating_moves else 0.0,
                    "new_best_after_deterioration": best_after_deterioration,
                    "final_temperature": round(temperature, 6),
                },
                "final_operator_weights": {
                    key: round(value, 6) for key, value in operator_weights.items()
                },
                "cache": cache.stats(),
                "elapsed_seconds": round(elapsed, 6),
            },
        },
    )
    return Q2LnsResult(
        final,
        tuple(logs),
        tuple(candidate_logs),
        tuple(operator_rows),
        elapsed,
    )
