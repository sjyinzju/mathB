from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

from ..rules import flight_minutes
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
            if _direct_time_lower_bound(data, left.base_airport, aircraft_type, service_order) > old_aircraft_time:
                continue
            cache_key = (left.base_airport, aircraft_type, service_order)
            augmentation = augmentation_cache.get(cache_key)
            if augmentation is None:
                augmentation = augment_service_sequence(
                    left.base_airport,
                    aircraft_type,
                    service_order,
                    matrix=data.matrix,
                    config=data.config,
                )
                augmentation_cache[cache_key] = augmentation
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
) -> Solution:
    """Deterministically merge under-filled same-base routes using exact evaluation."""
    solver_config = solver_config or SolverConfig()
    routes = list(solution.routes)
    evaluations = [evaluate_route(route, matrix=data.matrix, config=data.config) for route in routes]
    if any(not item.feasible for item in evaluations):
        raise ValueError("Input solution contains infeasible routes")
    initial_time = sum(item.total_aircraft_time_minutes for item in evaluations)
    pair_count = 0
    route_evaluation_count = 0
    accepted = 0
    augmentation_cache: dict[tuple[str, str, tuple[str, ...]], AugmentationResult] = {}

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
                left, right, old_time, data, augmentation_cache
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
