from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
from statistics import mean, median

from ..rules import flight_minutes
from .candidate_ranking import (
    CandidateRanker,
    RawDistanceRanker,
    route_service_nodes,
    route_signature,
)
from .data import ProblemData
from .evaluator import evaluate_route
from .models import (
    PassengerAssignment,
    AugmentationResult,
    RouteEvaluation,
    RoutePlan,
    Solution,
    SolverConfig,
    aggregate_evaluations,
)
from .technical_stops import augment_service_sequence


@dataclass(frozen=True)
class ImprovementStats:
    generated_pairs: int
    evaluated_pairs: int
    evaluated_routes: int
    feasible_pairs: int
    improving_pairs: int
    accepted_merges: int
    primary_improvement_minutes: int
    technical_stop_searches: int
    augmentation_cache_hits: int
    mean_feasible_saving_minutes: float
    median_feasible_saving_minutes: float
    ranker: str
    candidate_mode: str
    pair_budget: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_pairs": self.generated_pairs,
            "evaluated_pairs": self.evaluated_pairs,
            "evaluated_routes": self.evaluated_routes,
            "feasible_pairs": self.feasible_pairs,
            "improving_pairs": self.improving_pairs,
            "accepted_merges": self.accepted_merges,
            "primary_improvement_minutes": self.primary_improvement_minutes,
            "technical_stop_searches": self.technical_stop_searches,
            "augmentation_cache_hits": self.augmentation_cache_hits,
            "mean_feasible_saving_minutes": self.mean_feasible_saving_minutes,
            "median_feasible_saving_minutes": self.median_feasible_saving_minutes,
            "ranker": self.ranker,
            "candidate_mode": self.candidate_mode,
            "pair_budget": self.pair_budget,
        }


@dataclass(frozen=True)
class MergeSearchResult:
    route: RoutePlan | None
    evaluation: RouteEvaluation | None
    route_evaluations: int
    technical_stop_searches: int
    augmentation_cache_hits: int
    lower_bound_pruned: int
    augmentation_infeasible: int
    reason: str


def _score(evaluations: list[RouteEvaluation], order: tuple[str, ...]) -> tuple[float, ...]:
    numerator = sum(item.seat_km_numerator for item in evaluations)
    denominator = sum(item.seat_km_denominator for item in evaluations)
    values = {
        "total_passenger_travel_time_minutes": float(
            sum(item.total_passenger_travel_time_minutes for item in evaluations)
        ),
        "total_flights": float(len(evaluations)),
        "total_fuel_consumption_kg": round(
            sum(item.total_fuel_consumption_kg for item in evaluations), 6
        ),
        "seat_utilization": -(numerator / denominator if denominator else 0.0),
    }
    return (
        float(sum(item.total_aircraft_time_minutes for item in evaluations)),
        *(values[name] for name in order),
    )


def _service_nodes(route: RoutePlan) -> tuple[str, ...]:
    return route_service_nodes(route)


def _direct_time_lower_bound(
    data: ProblemData,
    base: str,
    aircraft_type: str,
    service_order: tuple[str, ...],
) -> int:
    aircraft = data.config.aircraft_types[aircraft_type]
    nodes = (base, *service_order, base)
    return sum(
        flight_minutes(data.matrix[left][right], aircraft.speed_kmh)
        for left, right in zip(nodes, nodes[1:])
    ) + len(service_order) * data.config.stop_without_refuel_minutes


def _merge_candidate(
    left: RoutePlan,
    right: RoutePlan,
    old_aircraft_time: int,
    data: ProblemData,
    augmentation_cache: dict[tuple[str, str, tuple[str, ...]], AugmentationResult],
) -> MergeSearchResult:
    assignments = left.assignments + right.assignments
    service_nodes = tuple(sorted(set(_service_nodes(left)) | set(_service_nodes(right))))
    if len(service_nodes) > data.config.max_sea_landings:
        return MergeSearchResult(None, None, 0, 0, 0, 0, 0, "stop_limit_pruned")
    best_route: RoutePlan | None = None
    best_evaluation: RouteEvaluation | None = None
    route_evaluations = 0
    technical_stop_searches = 0
    augmentation_cache_hits = 0
    lower_bound_pruned = 0
    augmentation_infeasible = 0
    for service_order in permutations(service_nodes):
        for aircraft_type in sorted(data.config.aircraft_types):
            aircraft = data.config.aircraft_types[aircraft_type]
            if len(assignments) > aircraft.seats:
                continue
            if _direct_time_lower_bound(data, left.base_airport, aircraft_type, service_order) > old_aircraft_time:
                lower_bound_pruned += 1
                continue
            cache_key = (left.base_airport, aircraft_type, service_order)
            augmentation = augmentation_cache.get(cache_key)
            if augmentation is None:
                technical_stop_searches += 1
                augmentation = augment_service_sequence(
                    left.base_airport,
                    aircraft_type,
                    service_order,
                    matrix=data.matrix,
                    config=data.config,
                )
                augmentation_cache[cache_key] = augmentation
            else:
                augmentation_cache_hits += 1
            if not augmentation.feasible:
                augmentation_infeasible += 1
                continue
            locations = tuple(stop.facility_id for stop in augmentation.stops)
            merged_assignments = tuple(
                PassengerAssignment(
                    person_id=item.person_id,
                    origin_id=item.origin_id,
                    destination_id=item.destination_id,
                    pickup_stop_order=0,
                    delivery_stop_order=locations.index(item.destination_id, 1),
                )
                for item in assignments
            )
            route = RoutePlan(
                base_airport=left.base_airport,
                aircraft_type=aircraft_type,
                stops=augmentation.stops,
                assignments=merged_assignments,
                service_facilities=service_order,
            )
            evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
            route_evaluations += 1
            if not evaluation.feasible:
                continue
            key = (
                evaluation.total_aircraft_time_minutes,
                evaluation.total_passenger_travel_time_minutes,
                evaluation.total_fuel_consumption_kg,
                -evaluation.seat_utilization,
                aircraft_type,
                service_order,
            )
            if best_evaluation is None:
                best_route, best_evaluation, best_key = route, evaluation, key
            elif key < best_key:
                best_route, best_evaluation, best_key = route, evaluation, key
    if best_route is not None:
        reason = "feasible"
    elif lower_bound_pruned and not technical_stop_searches and not augmentation_cache_hits:
        reason = "lower_bound_pruned"
    elif augmentation_infeasible:
        reason = "technical_stop_infeasible"
    else:
        reason = "route_infeasible"
    return MergeSearchResult(
        best_route,
        best_evaluation,
        route_evaluations,
        technical_stop_searches,
        augmentation_cache_hits,
        lower_bound_pruned,
        augmentation_infeasible,
        reason,
    )


def _eligible_candidate_pairs(
    routes: list[RoutePlan], data: ProblemData
) -> list[tuple[int, int]]:
    maximum_capacity = max(aircraft.seats for aircraft in data.config.aircraft_types.values())
    return [
        (left_index, right_index)
        for left_index, left in enumerate(routes)
        for right_index in range(left_index + 1, len(routes))
        if (right := routes[right_index]).base_airport == left.base_airport
        and left.passenger_count + right.passenger_count <= maximum_capacity
    ]


def _candidate_pairs(
    routes: list[RoutePlan],
    data: ProblemData,
    max_neighbors: int,
    ranker: CandidateRanker,
    *,
    candidate_mode: str,
    pair_budget: int | None,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    eligible = _eligible_candidate_pairs(routes, data)
    if candidate_mode == "global":
        ranked = sorted(
            eligible,
            key=lambda pair: ranker.pair_key(
                routes[pair[0]], routes[pair[1]], pair[0], pair[1], data
            ),
        )
        selected = ranked if pair_budget is None else ranked[:pair_budget]
        return sorted(selected), ranked
    if candidate_mode != "legacy":
        raise ValueError("candidate_mode must be 'legacy' or 'global'")

    pairs: set[tuple[int, int]] = set()
    maximum_capacity = max(aircraft.seats for aircraft in data.config.aircraft_types.values())
    for index, route in enumerate(routes):
        ranked_neighbors: list[tuple[tuple[object, ...], int]] = []
        for other_index, other in enumerate(routes):
            if other_index == index or other.base_airport != route.base_airport:
                continue
            if route.passenger_count + other.passenger_count > maximum_capacity:
                continue
            ranked_neighbors.append(
                (ranker.rank_key(route, other, index, other_index, data), other_index)
            )
        for _, other_index in sorted(ranked_neighbors)[:max_neighbors]:
            pairs.add(tuple(sorted((index, other_index))))
    ranked = sorted(
        eligible,
        key=lambda pair: ranker.pair_key(
            routes[pair[0]], routes[pair[1]], pair[0], pair[1], data
        ),
    )
    return sorted(pairs), ranked


def _candidate_event(
    *,
    iteration: int,
    rank: int,
    selected: bool,
    left_index: int,
    right_index: int,
    routes: list[RoutePlan],
    data: ProblemData,
    ranker: CandidateRanker,
) -> dict[str, object]:
    left = routes[left_index]
    right = routes[right_index]
    features = ranker.features(left, right, data)
    return {
        "iteration": iteration,
        "ranker": ranker.name,
        "candidate_rank": rank,
        "selected": selected,
        "left_index": left_index,
        "right_index": right_index,
        "base_airport": left.base_airport,
        "left_signature": route_signature(left),
        "right_signature": route_signature(right),
        "left_load": left.passenger_count,
        "right_load": right.passenger_count,
        "left_services": "|".join(_service_nodes(left)),
        "right_services": "|".join(_service_nodes(right)),
        **features.to_dict(),
        "outcome": "pending" if selected else "budget_not_selected",
        "route_evaluations": 0,
        "technical_stop_searches": 0,
        "augmentation_cache_hits": 0,
        "lower_bound_pruned": 0,
        "augmentation_infeasible": 0,
        "saving_minutes": "",
        "accepted": False,
    }


def improve_q1_savings(
    solution: Solution,
    data: ProblemData,
    solver_config: SolverConfig | None = None,
    *,
    max_neighbors: int = 8,
    max_iterations: int = 100,
    candidate_ranker: CandidateRanker | None = None,
    candidate_mode: str = "legacy",
    pair_budget: int | None = None,
    candidate_events: list[dict[str, object]] | None = None,
) -> Solution:
    """Deterministically merge under-filled same-base routes using exact evaluation."""
    solver_config = solver_config or SolverConfig()
    ranker = candidate_ranker or RawDistanceRanker()
    if max_neighbors <= 0:
        raise ValueError("max_neighbors must be positive")
    if pair_budget is not None and pair_budget <= 0:
        raise ValueError("pair_budget must be positive when provided")
    if candidate_mode == "legacy" and pair_budget is not None:
        raise ValueError("pair_budget is only supported in global candidate mode")
    routes = list(solution.routes)
    evaluations = [evaluate_route(route, matrix=data.matrix, config=data.config) for route in routes]
    if any(not item.feasible for item in evaluations):
        raise ValueError("Input solution contains infeasible routes")
    initial_time = sum(item.total_aircraft_time_minutes for item in evaluations)
    generated_pair_count = 0
    pair_count = 0
    route_evaluation_count = 0
    feasible_pair_count = 0
    improving_pair_count = 0
    accepted = 0
    technical_stop_search_count = 0
    augmentation_cache_hit_count = 0
    feasible_savings: list[int] = []
    augmentation_cache: dict[tuple[str, str, tuple[str, ...]], AugmentationResult] = {}

    for iteration in range(max_iterations):
        current_score = _score(evaluations, solver_config.secondary_order)
        best_move = None
        best_score = current_score
        selected_pairs, ranked_pairs = _candidate_pairs(
            routes,
            data,
            max_neighbors,
            ranker,
            candidate_mode=candidate_mode,
            pair_budget=pair_budget,
        )
        generated_pair_count += len(ranked_pairs)
        selected_set = set(selected_pairs)
        event_by_pair: dict[tuple[int, int], dict[str, object]] = {}
        if candidate_events is not None:
            for rank, (left_index, right_index) in enumerate(ranked_pairs, start=1):
                event = _candidate_event(
                    iteration=iteration,
                    rank=rank,
                    selected=(left_index, right_index) in selected_set,
                    left_index=left_index,
                    right_index=right_index,
                    routes=routes,
                    data=data,
                    ranker=ranker,
                )
                candidate_events.append(event)
                event_by_pair[(left_index, right_index)] = event
        for left_index, right_index in selected_pairs:
            pair_count += 1
            left = routes[left_index]
            right = routes[right_index]
            old_time = (
                evaluations[left_index].total_aircraft_time_minutes
                + evaluations[right_index].total_aircraft_time_minutes
            )
            merge = _merge_candidate(
                left, right, old_time, data, augmentation_cache
            )
            route_evaluation_count += merge.route_evaluations
            technical_stop_search_count += merge.technical_stop_searches
            augmentation_cache_hit_count += merge.augmentation_cache_hits
            event = event_by_pair.get((left_index, right_index))
            if event is not None:
                event.update(
                    {
                        "route_evaluations": merge.route_evaluations,
                        "technical_stop_searches": merge.technical_stop_searches,
                        "augmentation_cache_hits": merge.augmentation_cache_hits,
                        "lower_bound_pruned": merge.lower_bound_pruned,
                        "augmentation_infeasible": merge.augmentation_infeasible,
                        "outcome": merge.reason,
                    }
                )
            if merge.route is None or merge.evaluation is None:
                continue
            candidate = merge.route
            candidate_evaluation = merge.evaluation
            feasible_pair_count += 1
            saving = old_time - candidate_evaluation.total_aircraft_time_minutes
            feasible_savings.append(saving)
            trial_evaluations = [
                item
                for index, item in enumerate(evaluations)
                if index not in {left_index, right_index}
            ] + [candidate_evaluation]
            trial_score = _score(trial_evaluations, solver_config.secondary_order)
            improving = trial_score < current_score
            improving_pair_count += int(improving)
            if event is not None:
                event["saving_minutes"] = saving
                event["outcome"] = "improving_not_selected" if improving else "non_improving"
            if trial_score < best_score:
                best_score = trial_score
                best_move = (
                    left_index,
                    right_index,
                    candidate,
                    candidate_evaluation,
                    event,
                )
        if best_move is None:
            break
        left_index, right_index, candidate, candidate_evaluation, accepted_event = best_move
        if accepted_event is not None:
            accepted_event["accepted"] = True
            accepted_event["outcome"] = "accepted"
        routes = [
            route for index, route in enumerate(routes) if index not in {left_index, right_index}
        ] + [candidate]
        evaluations = [
            item for index, item in enumerate(evaluations) if index not in {left_index, right_index}
        ] + [candidate_evaluation]
        accepted += 1

    metrics = aggregate_evaluations(evaluations, served=solution.metrics.served_passengers)
    stats = ImprovementStats(
        generated_pairs=generated_pair_count,
        evaluated_pairs=pair_count,
        evaluated_routes=route_evaluation_count,
        feasible_pairs=feasible_pair_count,
        improving_pairs=improving_pair_count,
        accepted_merges=accepted,
        primary_improvement_minutes=initial_time - metrics.total_aircraft_time_minutes,
        technical_stop_searches=technical_stop_search_count,
        augmentation_cache_hits=augmentation_cache_hit_count,
        mean_feasible_saving_minutes=(mean(feasible_savings) if feasible_savings else 0.0),
        median_feasible_saving_minutes=(median(feasible_savings) if feasible_savings else 0.0),
        ranker=ranker.name,
        candidate_mode=candidate_mode,
        pair_budget=pair_budget,
    )
    return Solution(
        routes=tuple(routes),
        metrics=metrics,
        method="q1_b1_generalized_savings",
        diagnostics={**solution.diagnostics, "generalized_savings": stats.to_dict()},
    )
