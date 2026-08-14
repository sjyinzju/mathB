from __future__ import annotations

from typing import Iterable

from ..config import ProblemConfig
from ..rules import EPSILON, flight_minutes, fuel_for_leg
from .models import AugmentationResult, RouteStop, ServiceVisit
from .physics import LegPhysics


class _Label:
    """Pareto label; plain slots class to keep the hot search loop cheap.

    Semantics are identical to the previous frozen dataclass: the dominance
    predicate and all arithmetic are unchanged, and ``path_key`` is computed
    once at construction instead of on every dominance comparison.
    Intermediate paths are stored as ``(node, refuel, is_service)`` triples and
    converted back to ``RouteStop`` objects only for the final result.
    """

    __slots__ = (
        "node",
        "service_index",
        "stops_used",
        "time_minutes",
        "fuel_kg",
        "fuel_burned_kg",
        "path",
        "path_key",
    )

    def __init__(
        self,
        node: str,
        service_index: int,
        stops_used: int,
        time_minutes: int,
        fuel_kg: float,
        fuel_burned_kg: float,
        path: tuple[tuple[str, bool, bool], ...],
    ) -> None:
        self.node = node
        self.service_index = service_index
        self.stops_used = stops_used
        self.time_minutes = time_minutes
        self.fuel_kg = fuel_kg
        self.fuel_burned_kg = fuel_burned_kg
        self.path = path
        self.path_key = tuple((item[0], int(item[1])) for item in path)


def _dominates(left: _Label, right: _Label) -> bool:
    weak = (
        left.time_minutes <= right.time_minutes
        and left.fuel_kg + EPSILON >= right.fuel_kg
        and left.fuel_burned_kg <= right.fuel_burned_kg + EPSILON
    )
    strict = (
        left.time_minutes < right.time_minutes
        or left.fuel_kg > right.fuel_kg + EPSILON
        or left.fuel_burned_kg + EPSILON < right.fuel_burned_kg
    )
    return weak and (strict or left.path_key <= right.path_key)


def _insert_label(labels: list[_Label], candidate: _Label) -> None:
    if any(_dominates(existing, candidate) for existing in labels):
        return
    labels[:] = [existing for existing in labels if not _dominates(candidate, existing)]
    labels.append(candidate)


def augment_service_sequence(
    base_airport: str,
    aircraft_type: str,
    ordered_service_visits: Iterable[ServiceVisit | str],
    *,
    matrix: dict[str, dict[str, float]],
    config: ProblemConfig,
    physics: LegPhysics | None = None,
    stop_limit: int | None = None,
    candidate_nodes: Iterable[str] | None = None,
) -> AugmentationResult:
    """Insert technical stops and refuels while preserving the service order.

    The search uses Pareto labels over elapsed aircraft time, remaining fuel and
    cumulative burn. It never replaces an original leg by a metric closure.

    ``physics`` is an optional precomputed leg lookup whose entries are produced
    by the exact same ``rules.flight_minutes`` / ``rules.fuel_for_leg`` formulas;
    providing it only removes repeated arithmetic, never changes results.
    """
    stop_limit = config.max_sea_landings if stop_limit is None else stop_limit
    if base_airport not in config.airports:
        return AugmentationResult(False, reason="AIRPORT_INCOMPATIBLE")
    if aircraft_type not in config.aircraft_types:
        return AugmentationResult(False, reason="UNKNOWN_AIRCRAFT_TYPE")
    service_nodes = tuple(
        visit.facility_id if isinstance(visit, ServiceVisit) else visit
        for visit in ordered_service_visits
    )
    if not service_nodes or any(node not in config.facilities for node in service_nodes):
        return AugmentationResult(False, reason="INVALID_SERVICE_SEQUENCE")
    if len(service_nodes) > stop_limit:
        return AugmentationResult(False, reason="STOP_LIMIT")

    aircraft = config.aircraft_types[aircraft_type]
    available = set(matrix.get(base_airport, {}))
    candidates = tuple(
        sorted(
            node
            for node in (candidate_nodes if candidate_nodes is not None else config.facilities)
            if node in config.facilities and node in available and node in matrix
        )
    )
    if any(node not in candidates for node in service_nodes):
        return AugmentationResult(False, reason="MISSING_DISTANCE_NODE")

    legs = physics.table_for(aircraft_type) if physics is not None else None
    tank_capacity = aircraft.tank_capacity_kg
    reserve = aircraft.reserve_kg
    speed = aircraft.speed_kmh
    dwell_no_refuel = config.stop_without_refuel_minutes
    dwell_refuel = config.stop_with_refuel_minutes
    refuel_facilities = config.refuel_facilities
    service_count = len(service_nodes)
    # Precomputed suffix sets of the remaining service nodes per service index.
    suffix_services = [set(service_nodes[index:]) for index in range(service_count + 1)]
    # Per node: (refuel_flag, dwell_minutes) options in the original order.
    node_options: dict[str, tuple[tuple[bool, int], ...]] = {}
    for node in candidates:
        options = ((False, dwell_no_refuel),)
        if node in refuel_facilities:
            options += ((True, dwell_refuel),)
        node_options[node] = options

    start = _Label(
        node=base_airport,
        service_index=0,
        stops_used=0,
        time_minutes=0,
        fuel_kg=tank_capacity,
        fuel_burned_kg=0.0,
        path=((base_airport, False, False),),
    )
    frontier: dict[tuple[str, int, int], list[_Label]] = {(base_airport, 0, 0): [start]}
    completed: list[tuple[int, float, tuple[tuple[str, int], ...], _Label]] = []

    for used in range(stop_limit):
        next_frontier: dict[tuple[str, int, int], list[_Label]] = {}
        for labels in frontier.values():
            for label in labels:
                if label.stops_used != used:
                    continue
                remaining_services = suffix_services[label.service_index]
                next_required = (
                    service_nodes[label.service_index] if label.service_index < service_count else None
                )
                label_time = label.time_minutes
                label_fuel = label.fuel_kg
                label_burned = label.fuel_burned_kg
                label_path = label.path
                label_node = label.node
                for node in candidates:
                    if node == label_node:
                        continue
                    if node in remaining_services and node != next_required:
                        continue
                    if legs is not None:
                        distance, leg_minutes, burned = legs[(label_node, node)]
                    else:
                        distance = matrix[label_node][node]
                        burned = fuel_for_leg(distance, aircraft)
                        leg_minutes = flight_minutes(distance, speed)
                    arrival_fuel = label_fuel - burned
                    if arrival_fuel + EPSILON < reserve:
                        continue
                    is_service = node == next_required
                    new_service_index = label.service_index + int(is_service)
                    for refuel, dwell in node_options[node]:
                        departure_fuel = tank_capacity if refuel else arrival_fuel
                        next_label = _Label(
                            node=node,
                            service_index=new_service_index,
                            stops_used=used + 1,
                            time_minutes=label_time + leg_minutes + dwell,
                            fuel_kg=departure_fuel,
                            fuel_burned_kg=label_burned + burned,
                            path=label_path + ((node, refuel, is_service),),
                        )
                        key = (node, new_service_index, used + 1)
                        bucket = next_frontier.setdefault(key, [])
                        _insert_label(bucket, next_label)

                        if new_service_index == service_count:
                            if legs is not None:
                                return_distance, return_minutes, return_burn = legs[(node, base_airport)]
                            else:
                                return_distance = matrix[node][base_airport]
                                return_burn = fuel_for_leg(return_distance, aircraft)
                                return_minutes = flight_minutes(return_distance, speed)
                            if departure_fuel - return_burn + EPSILON >= reserve:
                                total_time = next_label.time_minutes + return_minutes
                                total_burn = next_label.fuel_burned_kg + return_burn
                                path_key = next_label.path_key + ((base_airport, 0),)
                                completed.append((total_time, total_burn, path_key, next_label))
        frontier = next_frontier

    if not completed:
        return AugmentationResult(False, reason="NO_AUGMENTED_ROUTE")
    total_time, total_burn, _, best = min(completed, key=lambda item: (item[0], item[1], item[2]))
    stops = tuple(
        RouteStop(node, refuel=refuel, is_service=is_service)
        for node, refuel, is_service in best.path
    ) + (RouteStop(base_airport),)
    return AugmentationResult(
        feasible=True,
        stops=stops,
        total_aircraft_time_minutes=total_time,
        total_fuel_consumption_kg=round(total_burn, 6),
    )
