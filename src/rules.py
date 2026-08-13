from __future__ import annotations

import math
from dataclasses import dataclass

from .config import AircraftType, ProblemConfig


EPSILON = 1e-9


def flight_minutes(distance_km: float, speed_kmh: float) -> int:
    if distance_km < 0 or speed_kmh <= 0:
        raise ValueError("Distance must be non-negative and speed must be positive")
    return math.ceil(60.0 * distance_km / speed_kmh)


def fuel_for_leg(distance_km: float, aircraft: AircraftType) -> float:
    if distance_km < 0:
        raise ValueError("Distance must be non-negative")
    return distance_km * aircraft.burn_kg_per_km


def fuel_after_leg(current_fuel_kg: float, distance_km: float, aircraft: AircraftType) -> float:
    return current_fuel_kg - fuel_for_leg(distance_km, aircraft)


def can_take_leg(current_fuel_kg: float, distance_km: float, aircraft: AircraftType) -> bool:
    return fuel_after_leg(current_fuel_kg, distance_km, aircraft) + EPSILON >= aircraft.reserve_kg


def can_refuel(node: str, config: ProblemConfig) -> bool:
    return node in config.refuel_facilities


def refuel_to_full(aircraft: AircraftType) -> float:
    return aircraft.tank_capacity_kg


def minimum_stop_minutes(node: str, refuel: bool, config: ProblemConfig) -> int:
    if node in config.airports:
        if refuel:
            raise ValueError("Airport route rows must use refuel=0")
        return 0
    if node not in config.facilities:
        raise ValueError(f"Unknown stop node: {node}")
    if refuel:
        if not can_refuel(node, config):
            raise ValueError(f"Node {node} is not a refuel facility")
        return config.stop_with_refuel_minutes
    return config.stop_without_refuel_minutes


@dataclass(frozen=True)
class LoadUpdate:
    after_delivery: int
    after_pickup: int


def update_passenger_load(
    current_load: int,
    deliveries: int,
    pickups: int,
    capacity: int,
) -> LoadUpdate:
    if min(current_load, deliveries, pickups, capacity) < 0:
        raise ValueError("Passenger counts and capacity must be non-negative")
    after_delivery = current_load - deliveries
    if after_delivery < 0:
        raise ValueError("Cannot deliver more passengers than are on board")
    after_pickup = after_delivery + pickups
    if after_pickup > capacity:
        raise ValueError(f"Capacity exceeded: {after_pickup} > {capacity}")
    return LoadUpdate(after_delivery=after_delivery, after_pickup=after_pickup)


@dataclass(frozen=True)
class FuelStep:
    arrival_fuel_kg: float
    departure_fuel_kg: float


def advance_fuel(
    current_fuel_kg: float,
    distance_km: float,
    destination: str,
    refuel: bool,
    aircraft: AircraftType,
    config: ProblemConfig,
) -> FuelStep:
    arrival = fuel_after_leg(current_fuel_kg, distance_km, aircraft)
    if arrival + EPSILON < aircraft.reserve_kg:
        raise ValueError(
            f"Arrival reserve violated at {destination}: {arrival:.3f} < {aircraft.reserve_kg:.3f} kg"
        )
    if refuel:
        if not can_refuel(destination, config):
            raise ValueError(f"Cannot refuel at {destination}")
        departure = refuel_to_full(aircraft)
    else:
        departure = arrival
    return FuelStep(arrival_fuel_kg=arrival, departure_fuel_kg=departure)
