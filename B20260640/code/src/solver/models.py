from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable


DEFAULT_SECONDARY_ORDER = (
    "total_passenger_travel_time_minutes",
    "total_flights",
    "total_fuel_consumption_kg",
    "seat_utilization",
)


@dataclass(frozen=True)
class SolverConfig:
    seed: int = 0
    secondary_order: tuple[str, ...] = DEFAULT_SECONDARY_ORDER

    def __post_init__(self) -> None:
        allowed = set(DEFAULT_SECONDARY_ORDER)
        if len(self.secondary_order) != len(set(self.secondary_order)):
            raise ValueError("secondary_order cannot contain duplicates")
        if set(self.secondary_order) != allowed:
            raise ValueError(f"secondary_order must be a permutation of {sorted(allowed)}")


@dataclass(frozen=True)
class DemandPool:
    origin_id: str
    destination_id: str
    person_ids: tuple[str, ...]

    @property
    def quantity(self) -> int:
        return len(self.person_ids)


@dataclass(frozen=True)
class ServiceVisit:
    facility_id: str
    quantity: int = 0
    pool_key: tuple[str, str] | None = None


@dataclass(frozen=True)
class RouteStop:
    facility_id: str
    refuel: bool = False
    is_service: bool = False


@dataclass(frozen=True)
class PassengerAssignment:
    person_id: str
    origin_id: str
    destination_id: str
    pickup_stop_order: int
    delivery_stop_order: int


@dataclass(frozen=True)
class RoutePlan:
    base_airport: str
    aircraft_type: str
    stops: tuple[RouteStop, ...]
    assignments: tuple[PassengerAssignment, ...] = ()
    service_facilities: tuple[str, ...] = ()

    @property
    def passenger_count(self) -> int:
        return len(self.assignments)


@dataclass(frozen=True)
class LegEvaluation:
    origin: str
    destination: str
    distance_km: float
    flight_minutes: int
    arrival_fuel_kg: float
    departure_fuel_kg: float
    departure_load: int


@dataclass(frozen=True)
class RouteEvaluation:
    feasible: bool
    issues: tuple[str, ...]
    total_aircraft_time_minutes: int
    total_passenger_travel_time_minutes: int
    total_fuel_consumption_kg: float
    seat_km_numerator: float
    seat_km_denominator: float
    legs: tuple[LegEvaluation, ...] = ()

    @property
    def seat_utilization(self) -> float:
        return self.seat_km_numerator / self.seat_km_denominator if self.seat_km_denominator else 0.0


@dataclass(frozen=True)
class AugmentationResult:
    feasible: bool
    stops: tuple[RouteStop, ...] = ()
    total_aircraft_time_minutes: int = 0
    total_fuel_consumption_kg: float = 0.0
    reason: str | None = None


@dataclass(frozen=True)
class SolutionMetrics:
    total_aircraft_time_minutes: int
    total_passenger_travel_time_minutes: int
    total_flights: int
    total_fuel_consumption_kg: float
    seat_utilization: float
    served_passengers: int

    def comparison_key(
        self, secondary_order: tuple[str, ...] = DEFAULT_SECONDARY_ORDER
    ) -> tuple[float, ...]:
        values = {
            "total_passenger_travel_time_minutes": float(self.total_passenger_travel_time_minutes),
            "total_flights": float(self.total_flights),
            "total_fuel_consumption_kg": float(self.total_fuel_consumption_kg),
            "seat_utilization": -float(self.seat_utilization),
        }
        return (float(self.total_aircraft_time_minutes), *(values[name] for name in secondary_order))

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Solution:
    routes: tuple[RoutePlan, ...]
    metrics: SolutionMetrics
    method: str = "q1_b0_single_facility_dp"
    diagnostics: dict[str, object] = field(default_factory=dict)


def aggregate_evaluations(evaluations: Iterable[RouteEvaluation], served: int) -> SolutionMetrics:
    values = tuple(evaluations)
    numerator = sum(item.seat_km_numerator for item in values)
    denominator = sum(item.seat_km_denominator for item in values)
    return SolutionMetrics(
        total_aircraft_time_minutes=sum(item.total_aircraft_time_minutes for item in values),
        total_passenger_travel_time_minutes=sum(item.total_passenger_travel_time_minutes for item in values),
        total_flights=len(values),
        total_fuel_consumption_kg=round(sum(item.total_fuel_consumption_kg for item in values), 6),
        seat_utilization=numerator / denominator if denominator else 0.0,
        served_passengers=served,
    )
