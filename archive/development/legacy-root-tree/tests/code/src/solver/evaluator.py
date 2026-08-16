from __future__ import annotations

from collections import Counter

from ..config import ProblemConfig
from ..rules import EPSILON, flight_minutes, fuel_for_leg, minimum_stop_minutes
from .models import LegEvaluation, RouteEvaluation, RoutePlan


def _expected_node(node: str, base_airport: str) -> str:
    return base_airport if node == "LAND" else node


def evaluate_route(
    route: RoutePlan,
    *,
    matrix: dict[str, dict[str, float]],
    config: ProblemConfig,
) -> RouteEvaluation:
    issues: list[str] = []
    if route.aircraft_type not in config.aircraft_types:
        return RouteEvaluation(False, ("UNKNOWN_AIRCRAFT_TYPE",), 0, 0, 0.0, 0.0, 0.0)
    aircraft = config.aircraft_types[route.aircraft_type]
    locations = tuple(stop.facility_id for stop in route.stops)
    if len(locations) < 3:
        issues.append("ROUTE_TOO_SHORT")
    if not locations or locations[0] != route.base_airport or locations[-1] != route.base_airport:
        issues.append("ROUTE_HOME_MISMATCH")
    if route.base_airport not in config.airports:
        issues.append("AIRPORT_INCOMPATIBLE")
    if len(locations[1:-1]) > config.max_sea_landings:
        issues.append("STOP_LIMIT")
    if any(node not in config.facilities for node in locations[1:-1]):
        issues.append("INVALID_SEA_STOP")
    if route.stops and (route.stops[0].refuel or route.stops[-1].refuel):
        issues.append("AIRPORT_REFUEL_FLAG")

    pickups: Counter[int] = Counter()
    deliveries: Counter[int] = Counter()
    valid_assignments = []
    for assignment in route.assignments:
        pickup = assignment.pickup_stop_order
        delivery = assignment.delivery_stop_order
        if not (0 <= pickup < delivery < len(route.stops)):
            issues.append(f"PICKUP_DELIVERY_ORDER:{assignment.person_id}")
            continue
        if locations[pickup] != _expected_node(assignment.origin_id, route.base_airport):
            issues.append(f"PICKUP_NODE_MISMATCH:{assignment.person_id}")
        expected_destination = _expected_node(assignment.destination_id, route.base_airport)
        if locations[delivery] != expected_destination:
            issues.append(f"DELIVERY_NODE_MISMATCH:{assignment.person_id}")
        first_destination = next(
            (index for index in range(pickup + 1, len(locations)) if locations[index] == expected_destination),
            None,
        )
        if first_destination != delivery:
            issues.append(f"NOT_FIRST_DESTINATION_STOP:{assignment.person_id}")
        pickups[pickup] += 1
        deliveries[delivery] += 1
        valid_assignments.append(assignment)

    arrivals = [0] * len(locations)
    departures = [0] * len(locations)
    fuel = aircraft.tank_capacity_kg
    clock = 0
    total_burn = 0.0
    load = 0
    numerator = 0.0
    denominator = 0.0
    legs: list[LegEvaluation] = []

    for index in range(len(locations)):
        load -= deliveries[index]
        if load < 0:
            issues.append(f"NEGATIVE_PASSENGER_LOAD:{index}")
            load = 0
        load += pickups[index]
        if load > aircraft.seats:
            issues.append(f"CAPACITY_VIOLATION:{index}")
        if index == len(locations) - 1:
            break
        origin = locations[index]
        destination = locations[index + 1]
        try:
            distance = matrix[origin][destination]
        except KeyError:
            issues.append(f"MISSING_DISTANCE:{origin}->{destination}")
            continue
        minutes = flight_minutes(distance, aircraft.speed_kmh)
        burned = fuel_for_leg(distance, aircraft)
        arrival_fuel = fuel - burned
        total_burn += burned
        if arrival_fuel + EPSILON < aircraft.reserve_kg:
            issues.append(f"FUEL_RESERVE:{index + 1}")
        clock += minutes
        arrivals[index + 1] = clock
        next_stop = route.stops[index + 1]
        departure_fuel = arrival_fuel
        if index + 1 < len(locations) - 1:
            try:
                dwell = minimum_stop_minutes(destination, next_stop.refuel, config)
            except ValueError:
                issues.append(f"INVALID_REFUEL_LOCATION:{destination}")
                dwell = config.stop_without_refuel_minutes
            clock += dwell
            departures[index + 1] = clock
            if next_stop.refuel and destination in config.refuel_facilities:
                departure_fuel = aircraft.tank_capacity_kg
        numerator += load * distance
        denominator += aircraft.seats * distance
        legs.append(
            LegEvaluation(
                origin=origin,
                destination=destination,
                distance_km=distance,
                flight_minutes=minutes,
                arrival_fuel_kg=arrival_fuel,
                departure_fuel_kg=departure_fuel,
                departure_load=load,
            )
        )
        fuel = departure_fuel

    if load != 0:
        issues.append("PASSENGERS_REMAIN_ON_BOARD")
    passenger_time = sum(
        arrivals[item.delivery_stop_order] - departures[item.pickup_stop_order]
        for item in valid_assignments
    )
    return RouteEvaluation(
        feasible=not issues,
        issues=tuple(issues),
        total_aircraft_time_minutes=clock,
        total_passenger_travel_time_minutes=passenger_time,
        total_fuel_consumption_kg=round(total_burn, 6),
        seat_km_numerator=numerator,
        seat_km_denominator=denominator,
        legs=tuple(legs),
    )
