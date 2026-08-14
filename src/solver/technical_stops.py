from __future__ import annotations

from typing import Iterable

from ..config import ProblemConfig
from ..rules import EPSILON, flight_minutes, fuel_for_leg
from .models import AugmentationResult, RouteStop, ServiceVisit
from .physics import LegPhysics


# Label layout: (node, service_index, stops_used, time_minutes, fuel_kg,
#                fuel_burned_kg, path, path_key).
# Plain tuples keep the hot search loop at C-level construction speed; the
# dominance predicate and all arithmetic stay identical to the original
# dataclass implementation. Intermediate paths are stored as
# ``(node, refuel, is_service)`` triples and converted back to ``RouteStop``
# objects only for the final result.
_NODE = 0
_SERVICE_INDEX = 1
_STOPS_USED = 2
_TIME = 3
_FUEL = 4
_BURNED = 5
_PATH = 6
_PATH_KEY = 7


def _dominates(left: tuple, right: tuple) -> bool:
    weak = (
        left[_TIME] <= right[_TIME]
        and left[_FUEL] + EPSILON >= right[_FUEL]
        and left[_BURNED] <= right[_BURNED] + EPSILON
    )
    strict = (
        left[_TIME] < right[_TIME]
        or left[_FUEL] > right[_FUEL] + EPSILON
        or left[_BURNED] + EPSILON < right[_BURNED]
    )
    return weak and (strict or left[_PATH_KEY] <= right[_PATH_KEY])


def _insert_label(labels: list[tuple], candidate: tuple) -> None:
    # Inlined dominance checks avoid one Python call frame per compared pair;
    # the predicate is exactly ``_dominates`` above.
    cand_time = candidate[_TIME]
    cand_fuel = candidate[_FUEL]
    cand_burned = candidate[_BURNED]
    cand_key = candidate[_PATH_KEY]
    for existing in labels:
        ex_time = existing[_TIME]
        ex_fuel = existing[_FUEL]
        ex_burned = existing[_BURNED]
        weak = (
            ex_time <= cand_time
            and ex_fuel + EPSILON >= cand_fuel
            and ex_burned <= cand_burned + EPSILON
        )
        if weak and (
            ex_time < cand_time
            or ex_fuel > cand_fuel + EPSILON
            or ex_burned + EPSILON < cand_burned
            or existing[_PATH_KEY] <= cand_key
        ):
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

    start = (
        base_airport,
        0,
        0,
        0,
        tank_capacity,
        0.0,
        ((base_airport, False, False),),
        ((base_airport, 0),),
    )
    frontier: dict[tuple[str, int, int], list[tuple]] = {(base_airport, 0, 0): [start]}
    completed: list[tuple[int, float, tuple[tuple[str, int], ...], tuple]] = []

    for used in range(stop_limit):
        next_frontier: dict[tuple[str, int, int], list[tuple]] = {}
        for labels in frontier.values():
            for label in labels:
                if label[_STOPS_USED] != used:
                    continue
                label_service_index = label[_SERVICE_INDEX]
                remaining_services = suffix_services[label_service_index]
                next_required = (
                    service_nodes[label_service_index] if label_service_index < service_count else None
                )
                label_time = label[_TIME]
                label_fuel = label[_FUEL]
                label_burned = label[_BURNED]
                label_path = label[_PATH]
                label_path_key = label[_PATH_KEY]
                label_node = label[_NODE]
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
                    new_service_index = label_service_index + int(is_service)
                    for refuel, dwell in node_options[node]:
                        departure_fuel = tank_capacity if refuel else arrival_fuel
                        path = label_path + ((node, refuel, is_service),)
                        next_label = (
                            node,
                            new_service_index,
                            used + 1,
                            label_time + leg_minutes + dwell,
                            departure_fuel,
                            label_burned + burned,
                            path,
                            label_path_key + ((node, int(refuel)),),
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
                                total_time = next_label[_TIME] + return_minutes
                                total_burn = next_label[_BURNED] + return_burn
                                path_key = next_label[_PATH_KEY] + ((base_airport, 0),)
                                completed.append((total_time, total_burn, path_key, next_label))
        frontier = next_frontier

    if not completed:
        return AugmentationResult(False, reason="NO_AUGMENTED_ROUTE")
    total_time, total_burn, _, best = min(completed, key=lambda item: (item[0], item[1], item[2]))
    stops = tuple(
        RouteStop(node, refuel=refuel, is_service=is_service)
        for node, refuel, is_service in best[_PATH]
    ) + (RouteStop(base_airport),)
    return AugmentationResult(
        feasible=True,
        stops=stops,
        total_aircraft_time_minutes=total_time,
        total_fuel_consumption_kg=round(total_burn, 6),
    )
