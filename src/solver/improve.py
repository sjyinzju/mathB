from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
from collections import Counter, defaultdict

from .cache import SolverCache
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
    evaluated_pairs: int
    evaluated_routes: int
    accepted_merges: int
    primary_improvement_minutes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "evaluated_pairs": self.evaluated_pairs,
            "evaluated_routes": self.evaluated_routes,
            "accepted_merges": self.accepted_merges,
            "primary_improvement_minutes": self.primary_improvement_minutes,
        }


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
    if route.service_facilities:
        return tuple(dict.fromkeys(route.service_facilities))
    return tuple(dict.fromkeys(item.destination_id for item in route.assignments))


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
) -> tuple[RoutePlan | None, RouteEvaluation | None, int]:
    assignments = left.assignments + right.assignments
    service_nodes = tuple(sorted(set(_service_nodes(left)) | set(_service_nodes(right))))
    if len(service_nodes) > data.config.max_sea_landings:
        return None, None, 0
    best_route: RoutePlan | None = None
    best_evaluation: RouteEvaluation | None = None
    route_evaluations = 0
    for service_order in permutations(service_nodes):
        for aircraft_type in sorted(data.config.aircraft_types):
            aircraft = data.config.aircraft_types[aircraft_type]
            if len(assignments) > aircraft.seats:
                continue
            if (
                _direct_time_lower_bound(data, left.base_airport, aircraft_type, service_order, cache)
                > old_aircraft_time
            ):
                continue
            augmentation = cache.augmentation_result(left.base_airport, aircraft_type, service_order)
            if not augmentation.feasible:
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
    return best_route, best_evaluation, route_evaluations


def _candidate_pairs(routes: list[RoutePlan], data: ProblemData, max_neighbors: int) -> list[tuple[int, int]]:
    pairs: set[tuple[int, int]] = set()
    maximum_capacity = max(aircraft.seats for aircraft in data.config.aircraft_types.values())
    for index, route in enumerate(routes):
        nodes = _service_nodes(route)
        ranked: list[tuple[float, int]] = []
        for other_index, other in enumerate(routes):
            if other_index == index or other.base_airport != route.base_airport:
                continue
            if route.passenger_count + other.passenger_count > maximum_capacity:
                continue
            other_nodes = _service_nodes(other)
            distance = min(data.matrix[left][right] for left in nodes for right in other_nodes)
            ranked.append((distance, other_index))
        for _, other_index in sorted(ranked)[:max_neighbors]:
            pairs.add(tuple(sorted((index, other_index))))
    return sorted(pairs)


def improve_q1_savings(
    solution: Solution,
    data: ProblemData,
    solver_config: SolverConfig | None = None,
    *,
    max_neighbors: int = 8,
    max_iterations: int = 100,
    cache: SolverCache | None = None,
) -> Solution:
    """Deterministically merge under-filled same-base routes using exact evaluation."""
    solver_config = solver_config or SolverConfig()
    cache = cache or SolverCache(data)
    routes = list(solution.routes)
    evaluations = [evaluate_route(route, matrix=data.matrix, config=data.config) for route in routes]
    if any(not item.feasible for item in evaluations):
        raise ValueError("Input solution contains infeasible routes")
    initial_time = sum(item.total_aircraft_time_minutes for item in evaluations)
    pair_count = 0
    route_evaluation_count = 0
    accepted = 0

    for _ in range(max_iterations):
        current_score = _score(evaluations, solver_config.secondary_order)
        best_move = None
        best_score = current_score
        for left_index, right_index in _candidate_pairs(routes, data, max_neighbors):
            pair_count += 1
            left = routes[left_index]
            right = routes[right_index]
            old_time = (
                evaluations[left_index].total_aircraft_time_minutes
                + evaluations[right_index].total_aircraft_time_minutes
            )
            candidate, candidate_evaluation, count = _merge_candidate(
                left, right, old_time, data, cache
            )
            route_evaluation_count += count
            if candidate is None or candidate_evaluation is None:
                continue
            trial_evaluations = [
                item
                for index, item in enumerate(evaluations)
                if index not in {left_index, right_index}
            ] + [candidate_evaluation]
            trial_score = _score(trial_evaluations, solver_config.secondary_order)
            if trial_score < best_score:
                best_score = trial_score
                best_move = (left_index, right_index, candidate, candidate_evaluation)
        if best_move is None:
            break
        left_index, right_index, candidate, candidate_evaluation = best_move
        routes = [
            route for index, route in enumerate(routes) if index not in {left_index, right_index}
        ] + [candidate]
        evaluations = [
            item for index, item in enumerate(evaluations) if index not in {left_index, right_index}
        ] + [candidate_evaluation]
        accepted += 1

    metrics = aggregate_evaluations(evaluations, served=solution.metrics.served_passengers)
    stats = ImprovementStats(
        evaluated_pairs=pair_count,
        evaluated_routes=route_evaluation_count,
        accepted_merges=accepted,
        primary_improvement_minutes=initial_time - metrics.total_aircraft_time_minutes,
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
