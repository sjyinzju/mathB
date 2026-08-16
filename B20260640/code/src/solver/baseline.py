from __future__ import annotations

from dataclasses import dataclass

from ..rules import flight_minutes, minimum_stop_minutes
from .cache import SolverCache
from .data import ProblemData
from .evaluator import evaluate_route
from .models import (
    PassengerAssignment,
    RoutePlan,
    Solution,
    SolverConfig,
    aggregate_evaluations,
)


class BaselineConstructionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class _Template:
    base: str
    destination: str
    aircraft_type: str
    stops: tuple
    capacity: int
    aircraft_time: int
    passenger_minutes: int
    fuel_kg: float
    passenger_distance_km: float
    total_distance_km: float
    delivery_order: int


@dataclass(frozen=True)
class _PackPlan:
    aircraft_time: int = 0
    passenger_time: int = 0
    flights: int = 0
    fuel_kg: float = 0.0
    numerator: float = 0.0
    denominator: float = 0.0
    loads: tuple[tuple[str, int], ...] = ()

    @property
    def utilization(self) -> float:
        return self.numerator / self.denominator if self.denominator else 0.0

    def score(self, secondary_order: tuple[str, ...]) -> tuple[object, ...]:
        values = {
            "total_passenger_travel_time_minutes": self.passenger_time,
            "total_flights": self.flights,
            "total_fuel_consumption_kg": round(self.fuel_kg, 6),
            "seat_utilization": -self.utilization,
        }
        return (self.aircraft_time, *(values[name] for name in secondary_order), self.loads)


def _template(
    data: ProblemData,
    base: str,
    destination: str,
    aircraft_type: str,
    cache: SolverCache,
) -> _Template | None:
    result = cache.augmentation_result(base, aircraft_type, (destination,))
    if not result.feasible:
        return None
    locations = tuple(stop.facility_id for stop in result.stops)
    delivery_order = locations.index(destination, 1)
    aircraft = data.config.aircraft_types[aircraft_type]
    clock = 0
    passenger_minutes = 0
    passenger_distance = 0.0
    total_distance = 0.0
    for index, (origin, target) in enumerate(zip(locations, locations[1:])):
        distance = data.matrix[origin][target]
        total_distance += distance
        clock += flight_minutes(distance, aircraft.speed_kmh)
        if index + 1 <= delivery_order:
            passenger_distance += distance
        if index + 1 == delivery_order:
            passenger_minutes = clock
        if index + 1 < len(locations) - 1:
            clock += minimum_stop_minutes(target, result.stops[index + 1].refuel, data.config)
    return _Template(
        base=base,
        destination=destination,
        aircraft_type=aircraft_type,
        stops=result.stops,
        capacity=aircraft.seats,
        aircraft_time=result.total_aircraft_time_minutes,
        passenger_minutes=passenger_minutes,
        fuel_kg=result.total_fuel_consumption_kg,
        passenger_distance_km=passenger_distance,
        total_distance_km=total_distance,
        delivery_order=delivery_order,
    )


def _pack_table(
    templates: dict[str, _Template], maximum: int, secondary_order: tuple[str, ...]
) -> list[_PackPlan | None]:
    table: list[_PackPlan | None] = [None] * (maximum + 1)
    table[0] = _PackPlan()
    for quantity in range(1, maximum + 1):
        best: _PackPlan | None = None
        for aircraft_type in sorted(templates):
            template = templates[aircraft_type]
            for load in range(1, min(template.capacity, quantity) + 1):
                previous = table[quantity - load]
                if previous is None:
                    continue
                candidate = _PackPlan(
                    aircraft_time=previous.aircraft_time + template.aircraft_time,
                    passenger_time=previous.passenger_time + load * template.passenger_minutes,
                    flights=previous.flights + 1,
                    fuel_kg=previous.fuel_kg + template.fuel_kg,
                    numerator=previous.numerator + load * template.passenger_distance_km,
                    denominator=previous.denominator + template.capacity * template.total_distance_km,
                    loads=previous.loads + ((aircraft_type, load),),
                )
                if best is None or candidate.score(secondary_order) < best.score(secondary_order):
                    best = candidate
        table[quantity] = best
    return table


def _combined_score(
    plans: tuple[_PackPlan, ...], secondary_order: tuple[str, ...]
) -> tuple[object, ...]:
    numerator = sum(plan.numerator for plan in plans)
    denominator = sum(plan.denominator for plan in plans)
    utilization = numerator / denominator if denominator else 0.0
    values = {
        "total_passenger_travel_time_minutes": sum(plan.passenger_time for plan in plans),
        "total_flights": sum(plan.flights for plan in plans),
        "total_fuel_consumption_kg": round(sum(plan.fuel_kg for plan in plans), 6),
        "seat_utilization": -utilization,
    }
    return (
        sum(plan.aircraft_time for plan in plans),
        *(values[name] for name in secondary_order),
        tuple(plan.loads for plan in plans),
    )


def _allocate_facility(
    data: ProblemData,
    destination: str,
    templates: dict[tuple[str, str], _Template],
    secondary_order: tuple[str, ...],
) -> tuple[dict[str, list[str]], dict[str, _PackPlan]]:
    airports = data.config.airports
    fixed_people = {
        airport: list(data.q1_pools.get((airport, destination), _empty_pool(airport, destination)).person_ids)
        for airport in airports
    }
    land_people = list(data.q1_pools.get(("LAND", destination), _empty_pool("LAND", destination)).person_ids)
    maximum = len(land_people) + max(len(values) for values in fixed_people.values())
    tables: dict[str, list[_PackPlan | None]] = {}
    for airport in airports:
        airport_templates = {
            aircraft_type: templates[(airport, aircraft_type)]
            for aircraft_type in data.config.aircraft_types
            if (airport, aircraft_type) in templates
        }
        if not airport_templates:
            raise BaselineConstructionError("NO_AUGMENTED_ROUTE", f"No template for {airport}->{destination}")
        tables[airport] = _pack_table(airport_templates, maximum, secondary_order)

    best: tuple[tuple[object, ...], tuple[int, ...], tuple[_PackPlan, ...]] | None = None
    land_count = len(land_people)
    for first in range(land_count + 1):
        for second in range(land_count - first + 1):
            allocations = (first, second, land_count - first - second)
            selected: list[_PackPlan] = []
            feasible = True
            for airport, extra in zip(airports, allocations):
                quantity = len(fixed_people[airport]) + extra
                plan = tables[airport][quantity]
                if plan is None:
                    feasible = False
                    break
                selected.append(plan)
            if not feasible:
                continue
            plans = tuple(selected)
            candidate = (_combined_score(plans, secondary_order), allocations, plans)
            if best is None or candidate[0:2] < best[0:2]:
                best = candidate
    if best is None:
        raise BaselineConstructionError("PACKING_FAILURE", f"Could not allocate demand for {destination}")

    _, allocations, plans = best
    people_by_airport: dict[str, list[str]] = {}
    offset = 0
    for airport, extra in zip(airports, allocations):
        people_by_airport[airport] = fixed_people[airport] + land_people[offset : offset + extra]
        offset += extra
    return people_by_airport, dict(zip(airports, plans))


def _empty_pool(origin: str, destination: str):
    from .models import DemandPool

    return DemandPool(origin, destination, ())


def solve_q1_baseline(
    data: ProblemData,
    solver_config: SolverConfig | None = None,
    *,
    cache: SolverCache | None = None,
) -> Solution:
    solver_config = solver_config or SolverConfig()
    cache = cache or SolverCache(data)
    destinations = sorted({destination for _, destination in data.q1_pools})
    all_routes: list[RoutePlan] = []
    evaluations = []
    allocation_diagnostics: dict[str, dict[str, int]] = {}

    for destination in destinations:
        templates: dict[tuple[str, str], _Template] = {}
        for airport in data.config.airports:
            for aircraft_type in data.config.aircraft_types:
                candidate = _template(data, airport, destination, aircraft_type, cache)
                if candidate is not None:
                    templates[(airport, aircraft_type)] = candidate
        people_by_airport, plans = _allocate_facility(
            data, destination, templates, solver_config.secondary_order
        )
        allocation_diagnostics[destination] = {
            airport: len(people_by_airport[airport]) for airport in data.config.airports
        }
        for airport in data.config.airports:
            people = people_by_airport[airport]
            cursor = 0
            for aircraft_type, load in plans[airport].loads:
                template = templates[(airport, aircraft_type)]
                assigned_people = people[cursor : cursor + load]
                cursor += load
                assignments = tuple(
                    PassengerAssignment(
                        person_id=person_id,
                        origin_id=(
                            "LAND"
                            if person_id
                            in data.q1_pools.get(("LAND", destination), _empty_pool("LAND", destination)).person_ids
                            else airport
                        ),
                        destination_id=destination,
                        pickup_stop_order=0,
                        delivery_stop_order=template.delivery_order,
                    )
                    for person_id in assigned_people
                )
                route = RoutePlan(
                    base_airport=airport,
                    aircraft_type=aircraft_type,
                    stops=template.stops,
                    assignments=assignments,
                    service_facilities=(destination,),
                )
                evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
                if not evaluation.feasible:
                    raise BaselineConstructionError(
                        "ROUTE_EVALUATION_FAILURE",
                        f"{airport}/{aircraft_type}/{destination}: {evaluation.issues}",
                    )
                all_routes.append(route)
                evaluations.append(evaluation)
            if cursor != len(people):
                raise BaselineConstructionError(
                    "PERSON_MAPPING", f"Mapped {cursor}/{len(people)} people for {airport}->{destination}"
                )

    assigned = [assignment.person_id for route in all_routes for assignment in route.assignments]
    expected = sorted(person for pool in data.q1_pools.values() for person in pool.person_ids)
    if sorted(assigned) != expected or len(assigned) != len(set(assigned)):
        raise BaselineConstructionError("PERSON_MAPPING", "Assignments are missing, duplicated or unknown")
    metrics = aggregate_evaluations(evaluations, served=len(assigned))
    return Solution(
        routes=tuple(all_routes),
        metrics=metrics,
        diagnostics={
            "seed": solver_config.seed,
            "secondary_order": list(solver_config.secondary_order),
            "facility_airport_passengers": allocation_diagnostics,
            "template_count": len(destinations) * len(data.config.airports) * len(data.config.aircraft_types),
        },
    )
