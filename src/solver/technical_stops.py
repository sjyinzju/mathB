from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..config import ProblemConfig
from ..rules import EPSILON, flight_minutes, fuel_for_leg
from .models import AugmentationResult, RouteStop, ServiceVisit


@dataclass(frozen=True)
class _Label:
    node: str
    service_index: int
    stops_used: int
    time_minutes: int
    fuel_kg: float
    fuel_burned_kg: float
    path: tuple[RouteStop, ...]

    @property
    def path_key(self) -> tuple[tuple[str, int], ...]:
        return tuple((stop.facility_id, int(stop.refuel)) for stop in self.path)


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
    stop_limit: int | None = None,
    candidate_nodes: Iterable[str] | None = None,
) -> AugmentationResult:
    """Insert technical stops and refuels while preserving the service order.

    The search uses Pareto labels over elapsed aircraft time, remaining fuel and
    cumulative burn. It never replaces an original leg by a metric closure.
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

    start = _Label(
        node=base_airport,
        service_index=0,
        stops_used=0,
        time_minutes=0,
        fuel_kg=aircraft.tank_capacity_kg,
        fuel_burned_kg=0.0,
        path=(RouteStop(base_airport),),
    )
    frontier: dict[tuple[str, int, int], list[_Label]] = {(base_airport, 0, 0): [start]}
    completed: list[tuple[int, float, tuple[tuple[str, int], ...], _Label]] = []

    for used in range(stop_limit):
        next_frontier: dict[tuple[str, int, int], list[_Label]] = {}
        for labels in frontier.values():
            for label in labels:
                if label.stops_used != used:
                    continue
                remaining_services = set(service_nodes[label.service_index :])
                next_required = service_nodes[label.service_index] if label.service_index < len(service_nodes) else None
                for node in candidates:
                    if node == label.node:
                        continue
                    if node in remaining_services and node != next_required:
                        continue
                    distance = matrix[label.node][node]
                    burned = fuel_for_leg(distance, aircraft)
                    arrival_fuel = label.fuel_kg - burned
                    if arrival_fuel + EPSILON < aircraft.reserve_kg:
                        continue
                    is_service = node == next_required
                    new_service_index = label.service_index + int(is_service)
                    refuel_options = (False, True) if node in config.refuel_facilities else (False,)
                    for refuel in refuel_options:
                        departure_fuel = aircraft.tank_capacity_kg if refuel else arrival_fuel
                        dwell = config.stop_with_refuel_minutes if refuel else config.stop_without_refuel_minutes
                        next_label = _Label(
                            node=node,
                            service_index=new_service_index,
                            stops_used=used + 1,
                            time_minutes=label.time_minutes
                            + flight_minutes(distance, aircraft.speed_kmh)
                            + dwell,
                            fuel_kg=departure_fuel,
                            fuel_burned_kg=label.fuel_burned_kg + burned,
                            path=label.path + (RouteStop(node, refuel=refuel, is_service=is_service),),
                        )
                        key = (node, new_service_index, used + 1)
                        bucket = next_frontier.setdefault(key, [])
                        _insert_label(bucket, next_label)

                        if new_service_index == len(service_nodes):
                            return_distance = matrix[node][base_airport]
                            return_burn = fuel_for_leg(return_distance, aircraft)
                            if departure_fuel - return_burn + EPSILON >= aircraft.reserve_kg:
                                total_time = next_label.time_minutes + flight_minutes(
                                    return_distance, aircraft.speed_kmh
                                )
                                total_burn = next_label.fuel_burned_kg + return_burn
                                path_key = next_label.path_key + ((base_airport, 0),)
                                completed.append((total_time, total_burn, path_key, next_label))
        frontier = next_frontier

    if not completed:
        return AugmentationResult(False, reason="NO_AUGMENTED_ROUTE")
    total_time, total_burn, _, best = min(completed, key=lambda item: (item[0], item[1], item[2]))
    return AugmentationResult(
        feasible=True,
        stops=best.path + (RouteStop(base_airport),),
        total_aircraft_time_minutes=total_time,
        total_fuel_consumption_kg=round(total_burn, 6),
    )
