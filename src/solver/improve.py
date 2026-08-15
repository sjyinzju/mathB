from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
from collections import Counter, defaultdict
from statistics import mean, median

from .cache import SolverCache
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
    RouteEvaluation,
    RoutePlan,
    Solution,
    SolverConfig,
    aggregate_evaluations,
)


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


@dataclass(frozen=True)
class RelocationStats:
    candidate_moves: int
    lower_bound_pruned: int
    feasible_moves: int
    accepted_moves: int
    moved_passengers: int
    cross_airport_moves: int
    route_evaluations: int
    primary_improvement_minutes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "candidate_moves": self.candidate_moves,
            "lower_bound_pruned": self.lower_bound_pruned,
            "feasible_moves": self.feasible_moves,
            "accepted_moves": self.accepted_moves,
            "moved_passengers": self.moved_passengers,
            "cross_airport_moves": self.cross_airport_moves,
            "route_evaluations": self.route_evaluations,
            "primary_improvement_minutes": self.primary_improvement_minutes,
        }


@dataclass(frozen=True)
class EjectionStats:
    candidate_chains: int
    lower_bound_pruned: int
    feasible_chains: int
    accepted_chains: int
    eliminated_routes: int
    moved_passengers: int
    route_evaluations: int
    primary_improvement_minutes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "candidate_chains": self.candidate_chains,
            "lower_bound_pruned": self.lower_bound_pruned,
            "feasible_chains": self.feasible_chains,
            "accepted_chains": self.accepted_chains,
            "eliminated_routes": self.eliminated_routes,
            "moved_passengers": self.moved_passengers,
            "route_evaluations": self.route_evaluations,
            "primary_improvement_minutes": self.primary_improvement_minutes,
        }


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
    cache: SolverCache,
) -> int:
    key = (base, aircraft_type, service_order)
    cached = cache.direct_time.get(key)
    if cached is not None:
        cache.hit("direct_time")
        return cached
    cache.miss("direct_time")
    physics = cache.physics
    nodes = (base, *service_order, base)
    total = sum(
        physics.flight_minutes(aircraft_type, left, right)
        for left, right in zip(nodes, nodes[1:])
    ) + len(service_order) * data.config.stop_without_refuel_minutes
    cache.direct_time[key] = total
    return total


def _assignment_signature(
    assignments: tuple[PassengerAssignment, ...],
) -> tuple[tuple[str, str, int], ...]:
    counts = Counter((item.origin_id, item.destination_id) for item in assignments)
    return tuple(sorted((origin, destination, count) for (origin, destination), count in counts.items()))


def _route_lower_bound(
    base: str,
    assignments: tuple[PassengerAssignment, ...],
    data: ProblemData,
    cache: SolverCache,
) -> int:
    if not assignments:
        return 0
    signature = _assignment_signature(assignments)
    bound_key = (base, signature)
    cached = cache.lower_bound.get(bound_key)
    if cached is not None:
        cache.hit("lower_bound")
        return cached
    cache.miss("lower_bound")
    if any(item.origin_id not in {"LAND", base} for item in assignments):
        cache.lower_bound[bound_key] = 10**9
        return 10**9
    service_nodes = tuple(sorted({item.destination_id for item in assignments}))
    if len(service_nodes) > data.config.max_sea_landings:
        cache.lower_bound[bound_key] = 10**9
        return 10**9
    best = 10**9
    for service_order in permutations(service_nodes):
        for aircraft_type, aircraft in data.config.aircraft_types.items():
            if len(assignments) <= aircraft.seats:
                best = min(
                    best,
                    _direct_time_lower_bound(data, base, aircraft_type, service_order, cache),
                )
    cache.lower_bound[bound_key] = best
    return best


def _route_evaluation_key(
    evaluation: RouteEvaluation,
    aircraft_type: str,
    service_order: tuple[str, ...],
    secondary_order: tuple[str, ...],
) -> tuple[object, ...]:
    values = {
        "total_passenger_travel_time_minutes": evaluation.total_passenger_travel_time_minutes,
        "total_flights": 1,
        "total_fuel_consumption_kg": evaluation.total_fuel_consumption_kg,
        "seat_utilization": -evaluation.seat_utilization,
    }
    return (
        evaluation.total_aircraft_time_minutes,
        *(values[name] for name in secondary_order),
        aircraft_type,
        service_order,
    )


def _rebuild_route(
    base: str,
    assignments: tuple[PassengerAssignment, ...],
    data: ProblemData,
    secondary_order: tuple[str, ...],
    cache: SolverCache,
) -> tuple[RoutePlan | None, RouteEvaluation | None, int]:
    if not assignments:
        return None, None, 0
    if any(item.origin_id not in {"LAND", base} for item in assignments):
        return None, None, 0
    signature = _assignment_signature(assignments)
    cache_key = (secondary_order, base, signature)
    cached = cache.skeleton.get(cache_key, "missing")
    if cached != "missing":
        cache.hit("skeleton")
        if cached is None:
            return None, None, 0
        aircraft_type, stops, service_order = cached
        locations = tuple(stop.facility_id for stop in stops)
        rebuilt_assignments = tuple(
            PassengerAssignment(
                item.person_id,
                item.origin_id,
                item.destination_id,
                0,
                locations.index(item.destination_id, 1),
            )
            for item in assignments
        )
        route = RoutePlan(base, aircraft_type, stops, rebuilt_assignments, service_order)
        return route, evaluate_route(route, matrix=data.matrix, config=data.config), 1
    cache.miss("skeleton")

    service_nodes = tuple(sorted({item.destination_id for item in assignments}))
    if len(service_nodes) > data.config.max_sea_landings:
        cache.skeleton[cache_key] = None
        return None, None, 0
    best: tuple[RoutePlan, RouteEvaluation] | None = None
    best_key: tuple[object, ...] | None = None
    evaluation_count = 0
    for service_order in permutations(service_nodes):
        for aircraft_type in sorted(data.config.aircraft_types):
            aircraft = data.config.aircraft_types[aircraft_type]
            if len(assignments) > aircraft.seats:
                continue
            augmentation = cache.augmentation_result(base, aircraft_type, service_order)
            if not augmentation.feasible:
                continue
            locations = tuple(stop.facility_id for stop in augmentation.stops)
            rebuilt_assignments = tuple(
                PassengerAssignment(
                    item.person_id,
                    item.origin_id,
                    item.destination_id,
                    0,
                    locations.index(item.destination_id, 1),
                )
                for item in assignments
            )
            route = RoutePlan(
                base,
                aircraft_type,
                augmentation.stops,
                rebuilt_assignments,
                service_order,
            )
            evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
            evaluation_count += 1
            if not evaluation.feasible:
                continue
            candidate_key = _route_evaluation_key(
                evaluation, aircraft_type, service_order, secondary_order
            )
            if best_key is None or candidate_key < best_key:
                best = (route, evaluation)
                best_key = candidate_key
    if best is None:
        cache.skeleton[cache_key] = None
        return None, None, evaluation_count
    cache.skeleton[cache_key] = (
        best[0].aircraft_type,
        best[0].stops,
        best[0].service_facilities,
    )
    return best[0], best[1], evaluation_count


def _relocation_quantities(
    source_load: int,
    target_load: int,
    batch_size: int,
    maximum_capacity: int,
) -> tuple[int, ...]:
    maximum = min(batch_size, maximum_capacity - target_load)
    if maximum <= 0:
        return ()
    values = {1, maximum}
    capacities = (12, 16, maximum_capacity)
    for capacity in capacities:
        values.add(source_load - capacity)
        values.add(capacity - target_load)
    if batch_size <= maximum:
        values.add(batch_size)
    return tuple(sorted(value for value in values if 1 <= value <= maximum))


def _merge_candidate(
    left: RoutePlan,
    right: RoutePlan,
    old_aircraft_time: int,
    data: ProblemData,
    cache: SolverCache,
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
            if (
                _direct_time_lower_bound(data, left.base_airport, aircraft_type, service_order, cache)
                > old_aircraft_time
            ):
                lower_bound_pruned += 1
                continue
            cache_key = (left.base_airport, aircraft_type, service_order)
            was_hit = cache_key in cache.augmentation
            augmentation = cache.augmentation_result(left.base_airport, aircraft_type, service_order)
            if was_hit:
                augmentation_cache_hits += 1
            else:
                technical_stop_searches += 1
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
    cache: SolverCache | None = None,
) -> Solution:
    """Deterministically merge under-filled same-base routes using exact evaluation."""
    solver_config = solver_config or SolverConfig()
    cache = cache or SolverCache(data)
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
                left, right, old_time, data, cache
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


def improve_q1_batch_relocation(
    solution: Solution,
    data: ProblemData,
    solver_config: SolverConfig | None = None,
    *,
    max_targets_per_batch: int = 4,
    max_iterations: int = 30,
    cache: SolverCache | None = None,
) -> Solution:
    """Best-improvement VND step that moves demand batches between routes.

    Fixed-airport passengers can move only between routes at their airport.
    LAND passengers may move across airports. Both affected routes are rebuilt
    with joint service-order, aircraft, technical-stop and refuel optimization.
    """
    solver_config = solver_config or SolverConfig()
    cache = cache or SolverCache(data)
    routes = list(solution.routes)
    evaluations = [evaluate_route(route, matrix=data.matrix, config=data.config) for route in routes]
    if any(not evaluation.feasible for evaluation in evaluations):
        raise ValueError("Input solution contains infeasible routes")
    initial_time = sum(item.total_aircraft_time_minutes for item in evaluations)
    maximum_capacity = max(aircraft.seats for aircraft in data.config.aircraft_types.values())
    candidate_moves = 0
    lower_bound_pruned = 0
    feasible_moves = 0
    accepted_moves = 0
    moved_passengers = 0
    cross_airport_moves = 0
    route_evaluations = 0
    move_log: list[dict[str, object]] = []

    for _ in range(max_iterations):
        current_score = _score(evaluations, solver_config.secondary_order)
        best_score = current_score
        best_move = None
        for source_index, source in enumerate(routes):
            batches: dict[tuple[str, str], list[PassengerAssignment]] = defaultdict(list)
            for assignment in source.assignments:
                batches[(assignment.origin_id, assignment.destination_id)].append(assignment)
            for (origin_id, destination), batch in sorted(batches.items()):
                ranked_targets: list[tuple[float, int]] = []
                for target_index, target in enumerate(routes):
                    if target_index == source_index or target.passenger_count >= maximum_capacity:
                        continue
                    if origin_id != "LAND" and target.base_airport != origin_id:
                        continue
                    target_services = _service_nodes(target)
                    if len(set(target_services) | {destination}) > data.config.max_sea_landings:
                        continue
                    related_distance = min(
                        data.matrix[destination][node] for node in target_services
                    )
                    ranked_targets.append((related_distance, target_index))
                for _, target_index in sorted(ranked_targets)[:max_targets_per_batch]:
                    target = routes[target_index]
                    quantities = _relocation_quantities(
                        source.passenger_count,
                        target.passenger_count,
                        len(batch),
                        maximum_capacity,
                    )
                    for quantity in quantities:
                        candidate_moves += 1
                        moved = tuple(sorted(batch, key=lambda item: item.person_id)[:quantity])
                        moved_ids = {item.person_id for item in moved}
                        source_assignments = tuple(
                            item for item in source.assignments if item.person_id not in moved_ids
                        )
                        target_assignments = target.assignments + moved
                        old_pair_time = (
                            evaluations[source_index].total_aircraft_time_minutes
                            + evaluations[target_index].total_aircraft_time_minutes
                        )
                        lower_bound = _route_lower_bound(
                            source.base_airport, source_assignments, data, cache
                        ) + _route_lower_bound(target.base_airport, target_assignments, data, cache)
                        if lower_bound > old_pair_time:
                            lower_bound_pruned += 1
                            continue
                        rebuilt_source, source_evaluation, source_count = _rebuild_route(
                            source.base_airport,
                            source_assignments,
                            data,
                            solver_config.secondary_order,
                            cache,
                        )
                        rebuilt_target, target_evaluation, target_count = _rebuild_route(
                            target.base_airport,
                            target_assignments,
                            data,
                            solver_config.secondary_order,
                            cache,
                        )
                        route_evaluations += source_count + target_count
                        if rebuilt_target is None or target_evaluation is None:
                            continue
                        if source_assignments and (
                            rebuilt_source is None or source_evaluation is None
                        ):
                            continue
                        feasible_moves += 1
                        trial_evaluations = [
                            item
                            for index, item in enumerate(evaluations)
                            if index not in {source_index, target_index}
                        ]
                        if source_evaluation is not None:
                            trial_evaluations.append(source_evaluation)
                        trial_evaluations.append(target_evaluation)
                        trial_score = _score(trial_evaluations, solver_config.secondary_order)
                        move_key = (
                            trial_score,
                            source.base_airport,
                            target.base_airport,
                            destination,
                            quantity,
                            source_index,
                            target_index,
                        )
                        if trial_score < best_score or (
                            trial_score == best_score
                            and best_move is not None
                            and move_key < best_move[0]
                        ):
                            best_score = trial_score
                            best_move = (
                                move_key,
                                source_index,
                                target_index,
                                rebuilt_source,
                                source_evaluation,
                                rebuilt_target,
                                target_evaluation,
                                quantity,
                                destination,
                                source.base_airport,
                                target.base_airport,
                            )
        if best_move is None:
            break
        (
            _,
            source_index,
            target_index,
            rebuilt_source,
            source_evaluation,
            rebuilt_target,
            target_evaluation,
            quantity,
            destination,
            source_base,
            target_base,
        ) = best_move
        old_time = (
            evaluations[source_index].total_aircraft_time_minutes
            + evaluations[target_index].total_aircraft_time_minutes
        )
        new_time = target_evaluation.total_aircraft_time_minutes + (
            source_evaluation.total_aircraft_time_minutes if source_evaluation else 0
        )
        routes = [
            route for index, route in enumerate(routes) if index not in {source_index, target_index}
        ]
        evaluations = [
            item for index, item in enumerate(evaluations) if index not in {source_index, target_index}
        ]
        if rebuilt_source is not None and source_evaluation is not None:
            routes.append(rebuilt_source)
            evaluations.append(source_evaluation)
        routes.append(rebuilt_target)
        evaluations.append(target_evaluation)
        accepted_moves += 1
        moved_passengers += quantity
        cross_airport_moves += int(source_base != target_base)
        move_log.append(
            {
                "iteration": accepted_moves,
                "origin_base": source_base,
                "target_base": target_base,
                "destination": destination,
                "passengers": quantity,
                "aircraft_time_improvement_minutes": old_time - new_time,
            }
        )

    metrics = aggregate_evaluations(evaluations, served=solution.metrics.served_passengers)
    stats = RelocationStats(
        candidate_moves=candidate_moves,
        lower_bound_pruned=lower_bound_pruned,
        feasible_moves=feasible_moves,
        accepted_moves=accepted_moves,
        moved_passengers=moved_passengers,
        cross_airport_moves=cross_airport_moves,
        route_evaluations=route_evaluations,
        primary_improvement_minutes=initial_time - metrics.total_aircraft_time_minutes,
    )
    return Solution(
        routes=tuple(routes),
        metrics=metrics,
        method="q1_b2_batch_relocation_vnd",
        diagnostics={
            **solution.diagnostics,
            "batch_relocation": stats.to_dict(),
            "batch_relocation_moves": move_log,
        },
    )


def _split_quantities(
    total: int,
    first_load: int,
    second_load: int,
    maximum_capacity: int,
) -> tuple[int, ...]:
    lower = max(1, total - (maximum_capacity - second_load))
    upper = min(total - 1, maximum_capacity - first_load)
    if lower > upper:
        return ()
    values = {lower, upper}
    for capacity in (12, 16, maximum_capacity):
        values.add(capacity - first_load)
        values.add(total - (capacity - second_load))
    return tuple(sorted(value for value in values if lower <= value <= upper))


def improve_q1_route_ejection(
    solution: Solution,
    data: ProblemData,
    solver_config: SolverConfig | None = None,
    *,
    max_targets: int = 6,
    max_iterations: int = 15,
    cache: SolverCache | None = None,
) -> Solution:
    """Eliminate an all-LAND route by splitting it into two residual-capacity routes."""
    solver_config = solver_config or SolverConfig()
    cache = cache or SolverCache(data)
    routes = list(solution.routes)
    evaluations = [evaluate_route(route, matrix=data.matrix, config=data.config) for route in routes]
    if any(not item.feasible for item in evaluations):
        raise ValueError("Input solution contains infeasible routes")
    initial_time = sum(item.total_aircraft_time_minutes for item in evaluations)
    maximum_capacity = max(aircraft.seats for aircraft in data.config.aircraft_types.values())
    candidate_chains = 0
    lower_bound_pruned = 0
    feasible_chains = 0
    accepted_chains = 0
    moved_passengers = 0
    route_evaluations = 0
    move_log: list[dict[str, object]] = []

    for _ in range(max_iterations):
        current_score = _score(evaluations, solver_config.secondary_order)
        best_score = current_score
        best_move = None
        for source_index, source in enumerate(routes):
            if not source.assignments or any(item.origin_id != "LAND" for item in source.assignments):
                continue
            destinations = {item.destination_id for item in source.assignments}
            if len(destinations) != 1:
                continue
            destination = next(iter(destinations))
            ranked_targets: list[tuple[float, int]] = []
            for target_index, target in enumerate(routes):
                if target_index == source_index or target.passenger_count >= maximum_capacity:
                    continue
                target_services = _service_nodes(target)
                if len(set(target_services) | {destination}) > data.config.max_sea_landings:
                    continue
                distance = min(data.matrix[destination][node] for node in target_services)
                ranked_targets.append((distance, target_index))
            nearest = [index for _, index in sorted(ranked_targets)[:max_targets]]
            high_slack = [
                index
                for _, index in sorted(
                    (
                        (-(maximum_capacity - routes[index].passenger_count), index)
                        for _, index in ranked_targets
                    )
                )[:max_targets]
            ]
            target_indices = list(dict.fromkeys((*nearest, *high_slack)))
            for position, first_index in enumerate(target_indices):
                for second_index in target_indices[position + 1 :]:
                    first = routes[first_index]
                    second = routes[second_index]
                    split_values = _split_quantities(
                        source.passenger_count,
                        first.passenger_count,
                        second.passenger_count,
                        maximum_capacity,
                    )
                    for first_quantity in split_values:
                        candidate_chains += 1
                        ordered_source = tuple(sorted(source.assignments, key=lambda item: item.person_id))
                        first_moved = ordered_source[:first_quantity]
                        second_moved = ordered_source[first_quantity:]
                        first_assignments = first.assignments + first_moved
                        second_assignments = second.assignments + second_moved
                        old_time = (
                            evaluations[source_index].total_aircraft_time_minutes
                            + evaluations[first_index].total_aircraft_time_minutes
                            + evaluations[second_index].total_aircraft_time_minutes
                        )
                        lower_bound = _route_lower_bound(
                            first.base_airport, first_assignments, data, cache
                        ) + _route_lower_bound(second.base_airport, second_assignments, data, cache)
                        if lower_bound > old_time:
                            lower_bound_pruned += 1
                            continue
                        rebuilt_first, first_evaluation, first_count = _rebuild_route(
                            first.base_airport,
                            first_assignments,
                            data,
                            solver_config.secondary_order,
                            cache,
                        )
                        rebuilt_second, second_evaluation, second_count = _rebuild_route(
                            second.base_airport,
                            second_assignments,
                            data,
                            solver_config.secondary_order,
                            cache,
                        )
                        route_evaluations += first_count + second_count
                        if (
                            rebuilt_first is None
                            or first_evaluation is None
                            or rebuilt_second is None
                            or second_evaluation is None
                        ):
                            continue
                        feasible_chains += 1
                        trial_evaluations = [
                            item
                            for index, item in enumerate(evaluations)
                            if index not in {source_index, first_index, second_index}
                        ] + [first_evaluation, second_evaluation]
                        trial_score = _score(trial_evaluations, solver_config.secondary_order)
                        move_key = (
                            trial_score,
                            destination,
                            first.base_airport,
                            second.base_airport,
                            first_quantity,
                            source_index,
                            first_index,
                            second_index,
                        )
                        if trial_score < best_score or (
                            trial_score == best_score
                            and best_move is not None
                            and move_key < best_move[0]
                        ):
                            best_score = trial_score
                            best_move = (
                                move_key,
                                source_index,
                                first_index,
                                second_index,
                                rebuilt_first,
                                first_evaluation,
                                rebuilt_second,
                                second_evaluation,
                                destination,
                                first_quantity,
                                source.passenger_count - first_quantity,
                            )
        if best_move is None:
            break
        (
            _,
            source_index,
            first_index,
            second_index,
            rebuilt_first,
            first_evaluation,
            rebuilt_second,
            second_evaluation,
            destination,
            first_quantity,
            second_quantity,
        ) = best_move
        removed = {source_index, first_index, second_index}
        old_time = sum(evaluations[index].total_aircraft_time_minutes for index in removed)
        new_time = (
            first_evaluation.total_aircraft_time_minutes
            + second_evaluation.total_aircraft_time_minutes
        )
        source_base = routes[source_index].base_airport
        first_base = routes[first_index].base_airport
        second_base = routes[second_index].base_airport
        routes = [route for index, route in enumerate(routes) if index not in removed]
        evaluations = [item for index, item in enumerate(evaluations) if index not in removed]
        routes.extend((rebuilt_first, rebuilt_second))
        evaluations.extend((first_evaluation, second_evaluation))
        accepted_chains += 1
        moved_passengers += first_quantity + second_quantity
        move_log.append(
            {
                "iteration": accepted_chains,
                "source_base": source_base,
                "first_target_base": first_base,
                "second_target_base": second_base,
                "destination": destination,
                "first_passengers": first_quantity,
                "second_passengers": second_quantity,
                "aircraft_time_improvement_minutes": old_time - new_time,
            }
        )

    metrics = aggregate_evaluations(evaluations, served=solution.metrics.served_passengers)
    stats = EjectionStats(
        candidate_chains=candidate_chains,
        lower_bound_pruned=lower_bound_pruned,
        feasible_chains=feasible_chains,
        accepted_chains=accepted_chains,
        eliminated_routes=accepted_chains,
        moved_passengers=moved_passengers,
        route_evaluations=route_evaluations,
        primary_improvement_minutes=initial_time - metrics.total_aircraft_time_minutes,
    )
    return Solution(
        routes=tuple(routes),
        metrics=metrics,
        method="q1_b3_route_ejection_vnd",
        diagnostics={
            **solution.diagnostics,
            "route_ejection": stats.to_dict(),
            "route_ejection_moves": move_log,
        },
    )
