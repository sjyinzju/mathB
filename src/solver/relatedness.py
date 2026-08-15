from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from statistics import mean
from typing import Mapping, Sequence

from .candidate_ranking import (
    CandidateFeatures,
    FuelSignature,
    RawDistanceRanker,
    empirical_percentile_component,
    fuel_signature_gap,
    route_service_nodes,
    route_signature,
)
from .data import ProblemData
from .models import RoutePlan


SimilarityMatrix = Mapping[str, Mapping[str, float]]


@dataclass(frozen=True)
class ConsensusResult:
    facilities: tuple[str, ...]
    matrix: dict[str, dict[str, float]]
    configuration_names: tuple[str, ...]
    weights: tuple[float, ...]

    def to_dict(self) -> dict[str, object]:
        values = [
            self.matrix[left][right]
            for index, left in enumerate(self.facilities)
            for right in self.facilities[index + 1 :]
        ]
        return {
            "configuration_count": len(self.configuration_names),
            "configuration_names": list(self.configuration_names),
            "weights": list(self.weights),
            "minimum_pair_consensus": min(values),
            "maximum_pair_consensus": max(values),
            "mean_pair_consensus": mean(values),
            "distinct_pair_values": len(set(values)),
        }


def consensus_coassociation(
    facilities: Sequence[str],
    labelings: Mapping[str, Mapping[str, int]],
    *,
    weights: Mapping[str, float] | None = None,
) -> ConsensusResult:
    """Build a deterministic, stability-weightable co-association matrix."""

    facility_tuple = tuple(facilities)
    if not labelings:
        raise ValueError("at least one clustering is required")
    if len(facility_tuple) != len(set(facility_tuple)):
        raise ValueError("facilities must be unique")
    names = tuple(sorted(labelings))
    weight_values = tuple(float((weights or {}).get(name, 1.0)) for name in names)
    if any(value <= 0 for value in weight_values):
        raise ValueError("consensus weights must be positive")
    for name in names:
        if set(labelings[name]) != set(facility_tuple):
            raise ValueError(f"clustering {name} does not cover the facility set")
    total_weight = sum(weight_values)
    matrix = {left: {} for left in facility_tuple}
    for left in facility_tuple:
        for right in facility_tuple:
            matrix[left][right] = sum(
                weight
                for name, weight in zip(names, weight_values)
                if labelings[name][left] == labelings[name][right]
            ) / total_weight
    return ConsensusResult(facility_tuple, matrix, names, weight_values)


def consensus_leave_one_out_deviation(
    consensus: ConsensusResult,
    labelings: Mapping[str, Mapping[str, int]],
) -> dict[str, float]:
    """Mean absolute pair deviation after removing each consensus member."""

    deviations: dict[str, float] = {}
    base_pairs = [
        (left, right)
        for index, left in enumerate(consensus.facilities)
        for right in consensus.facilities[index + 1 :]
    ]
    weight_map = dict(zip(consensus.configuration_names, consensus.weights))
    for omitted in consensus.configuration_names:
        remaining = {
            name: labels for name, labels in labelings.items() if name != omitted
        }
        reduced = consensus_coassociation(
            consensus.facilities,
            remaining,
            weights={name: weight_map[name] for name in remaining},
        )
        deviations[omitted] = mean(
            abs(consensus.matrix[left][right] - reduced.matrix[left][right])
            for left, right in base_pairs
        )
    return deviations


def _pair_values(
    facilities: Sequence[str], function,
) -> dict[tuple[str, str], float]:
    return {
        tuple(sorted((left, right))): float(function(left, right))
        for index, left in enumerate(facilities)
        for right in facilities[index + 1 :]
    }


def _similarity_from_values(
    facilities: Sequence[str],
    values: Mapping[tuple[str, str], float],
    *,
    higher_is_better: bool = False,
) -> dict[str, dict[str, float]]:
    ranked_values = (
        {pair: -float(value) for pair, value in values.items()}
        if higher_is_better
        else values
    )
    percentiles = empirical_percentile_component(facilities, ranked_values)
    result = {
        left: {right: 1.0 - float(percentiles[left][right]) for right in facilities}
        for left in facilities
    }
    for facility in facilities:
        result[facility][facility] = 1.0
    return result


def minimum_seat_capacity(demand: int, capacities: Sequence[int]) -> int:
    if demand < 0:
        raise ValueError("demand cannot be negative")
    values = tuple(sorted(set(int(value) for value in capacities)))
    if not values or values[0] <= 0:
        raise ValueError("capacities must be positive")
    if demand == 0:
        return 0
    limit = demand + values[-1]
    reachable = {0}
    for total in range(limit + 1):
        if total not in reachable:
            continue
        for capacity in values:
            if total + capacity <= limit:
                reachable.add(total + capacity)
    return min(total for total in reachable if total >= demand)


@dataclass(frozen=True)
class CapacityPairEvidence:
    left_demand: int
    right_demand: int
    separate_seats: int
    combined_seats: int
    saved_seats: int
    combined_utilization: float


def capacity_pair_evidence(
    left_demand: int, right_demand: int, capacities: Sequence[int]
) -> CapacityPairEvidence:
    left_seats = minimum_seat_capacity(left_demand, capacities)
    right_seats = minimum_seat_capacity(right_demand, capacities)
    combined = minimum_seat_capacity(left_demand + right_demand, capacities)
    return CapacityPairEvidence(
        left_demand=left_demand,
        right_demand=right_demand,
        separate_seats=left_seats + right_seats,
        combined_seats=combined,
        saved_seats=left_seats + right_seats - combined,
        combined_utilization=(left_demand + right_demand) / combined,
    )


@dataclass(frozen=True)
class RelatednessComponents:
    facilities: tuple[str, ...]
    matrices: Mapping[str, SimilarityMatrix]
    capacity_evidence: Mapping[tuple[str, str], CapacityPairEvidence]

    def __post_init__(self) -> None:
        required = {"distance", "consensus", "airport", "fuel", "capacity"}
        if set(self.matrices) != required:
            raise ValueError(f"components must be exactly {sorted(required)}")
        expected = set(self.facilities)
        for name, matrix in self.matrices.items():
            if set(matrix) != expected or any(set(matrix[row]) != expected for row in expected):
                raise ValueError(f"component {name} does not cover the facility set")


def build_static_components(
    data: ProblemData,
    *,
    consensus: SimilarityMatrix,
    fuel_signatures: Mapping[str, FuelSignature],
) -> RelatednessComponents:
    """Build solution-independent percentile-normalized facility similarities."""

    facilities = tuple(data.config.facilities)
    airports = tuple(data.config.airports)
    distance_values = _pair_values(
        facilities, lambda left, right: data.matrix[left][right]
    )
    airport_values = _pair_values(
        facilities,
        lambda left, right: mean(
            abs(data.matrix[airport][left] - data.matrix[airport][right])
            for airport in airports
        ),
    )
    fuel_values = _pair_values(
        facilities,
        lambda left, right: fuel_signature_gap(
            fuel_signatures[left], fuel_signatures[right]
        ),
    )
    demand = {facility: 0 for facility in facilities}
    for pool in data.q1_pools.values():
        demand[pool.destination_id] += pool.quantity
    capacities = tuple(aircraft.seats for aircraft in data.config.aircraft_types.values())
    capacity_evidence = {
        tuple(sorted((left, right))): capacity_pair_evidence(
            demand[left], demand[right], capacities
        )
        for index, left in enumerate(facilities)
        for right in facilities[index + 1 :]
    }
    saved_seat_values = {
        pair: float(value.saved_seats) for pair, value in capacity_evidence.items()
    }
    utilization_values = {
        pair: value.combined_utilization for pair, value in capacity_evidence.items()
    }
    saved_similarity = _similarity_from_values(
        facilities, saved_seat_values, higher_is_better=True
    )
    utilization_similarity = _similarity_from_values(
        facilities, utilization_values, higher_is_better=True
    )
    capacity_similarity = {
        left: {
            right: mean(
                (saved_similarity[left][right], utilization_similarity[left][right])
            )
            for right in facilities
        }
        for left in facilities
    }
    return RelatednessComponents(
        facilities=facilities,
        matrices={
            "distance": _similarity_from_values(facilities, distance_values),
            "consensus": consensus,
            "airport": _similarity_from_values(facilities, airport_values),
            "fuel": _similarity_from_values(facilities, fuel_values),
            "capacity": capacity_similarity,
        },
        capacity_evidence=capacity_evidence,
    )


@dataclass(frozen=True)
class RelatednessFeatures:
    distance: float
    consensus: float
    airport: float
    fuel: float
    capacity: float
    score: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class StaticRelatednessModel:
    components: RelatednessComponents
    weights: Mapping[str, float]
    name: str = "static_relatedness"

    def __post_init__(self) -> None:
        if not self.weights:
            raise ValueError("at least one component must be enabled")
        if not set(self.weights) <= set(self.components.matrices):
            raise ValueError("unknown relatedness component")
        if any(float(value) < 0 for value in self.weights.values()):
            raise ValueError("weights must be non-negative")
        if abs(sum(float(value) for value in self.weights.values()) - 1.0) > 1e-9:
            raise ValueError("weights must sum to one")

    @classmethod
    def equal_weighted(
        cls,
        components: RelatednessComponents,
        enabled: Sequence[str],
        *,
        name: str | None = None,
    ) -> StaticRelatednessModel:
        names = tuple(enabled)
        if not names:
            raise ValueError("enabled cannot be empty")
        weight = 1.0 / len(names)
        return cls(
            components,
            {component: weight for component in names},
            name or "static_" + "_".join(names),
        )

    def facility_pair_features(self, left: str, right: str) -> RelatednessFeatures:
        values = {
            name: float(matrix[left][right])
            for name, matrix in self.components.matrices.items()
        }
        score = sum(float(weight) * values[name] for name, weight in self.weights.items())
        return RelatednessFeatures(score=score, **values)

    def relatedness(self, left: str, right: str) -> float:
        return self.facility_pair_features(left, right).score

    def route_pair_features(
        self, left_services: Sequence[str], right_services: Sequence[str]
    ) -> RelatednessFeatures:
        if not left_services or not right_services:
            raise ValueError("route relatedness requires non-empty service sets")
        candidates = [
            (self.facility_pair_features(left, right), left, right)
            for left in left_services
            for right in right_services
        ]
        return max(candidates, key=lambda item: (item[0].score, item[1], item[2]))[0]

    def relatedness_to_route(self, facility: str, route: RoutePlan) -> float:
        return max(
            self.relatedness(facility, other) for other in route_service_nodes(route)
        )


@dataclass(frozen=True)
class ContextCompatibilityFeatures:
    allowed: bool
    airport_compatible: float
    capacity_feasible: float
    stop_feasible: float
    capacity_fill: float
    stop_score: float
    source_elimination: float
    static_relatedness: float
    current_route_utilization: float
    score: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ContextCompatibility:
    static_model: StaticRelatednessModel

    def facility_to_route(
        self,
        facility: str,
        batch_size: int,
        origin_id: str,
        route: RoutePlan,
        data: ProblemData,
        *,
        source_route: RoutePlan | None = None,
    ) -> ContextCompatibilityFeatures:
        maximum_capacity = max(
            aircraft.seats for aircraft in data.config.aircraft_types.values()
        )
        airport_ok = origin_id == "LAND" or origin_id == route.base_airport
        capacity_ok = route.passenger_count + batch_size <= maximum_capacity
        services = set(route_service_nodes(route))
        stop_ok = facility in services or len(services) < data.config.max_sea_landings
        allowed = airport_ok and capacity_ok and stop_ok and batch_size > 0
        capacity_fill = (
            (route.passenger_count + batch_size) / maximum_capacity if capacity_ok else 0.0
        )
        new_stop_count = len(services | {facility})
        stop_score = (
            1.0 - (new_stop_count - 1) / max(1, data.config.max_sea_landings - 1)
            if stop_ok
            else 0.0
        )
        source_elimination = float(
            source_route is not None and batch_size == source_route.passenger_count
        )
        static_score = self.static_model.relatedness_to_route(facility, route)
        aircraft_capacity = data.config.aircraft_types[route.aircraft_type].seats
        current_utilization = min(1.0, route.passenger_count / aircraft_capacity)
        signals = [capacity_fill, stop_score, static_score]
        if source_route is not None:
            signals.append(source_elimination)
        score = mean(signals) if allowed else 0.0
        return ContextCompatibilityFeatures(
            allowed,
            float(airport_ok),
            float(capacity_ok),
            float(stop_ok),
            capacity_fill,
            stop_score,
            source_elimination,
            static_score,
            current_utilization,
            score,
        )

    def route_pair_from_values(
        self,
        left_services: Sequence[str],
        right_services: Sequence[str],
        left_load: int,
        right_load: int,
        left_base: str,
        right_base: str,
        data: ProblemData,
    ) -> ContextCompatibilityFeatures:
        maximum_capacity = max(
            aircraft.seats for aircraft in data.config.aircraft_types.values()
        )
        combined_load = left_load + right_load
        services = set(left_services) | set(right_services)
        airport_ok = left_base == right_base
        capacity_ok = combined_load <= maximum_capacity
        stop_ok = len(services) <= data.config.max_sea_landings
        allowed = airport_ok and capacity_ok and stop_ok
        capacity_fill = combined_load / maximum_capacity if capacity_ok else 0.0
        stop_score = (
            1.0 - (len(services) - 1) / max(1, data.config.max_sea_landings - 1)
            if stop_ok
            else 0.0
        )
        static_score = self.static_model.route_pair_features(
            left_services, right_services
        ).score
        score = mean((capacity_fill, stop_score)) if allowed else 0.0
        return ContextCompatibilityFeatures(
            allowed,
            float(airport_ok),
            float(capacity_ok),
            float(stop_ok),
            capacity_fill,
            stop_score,
            1.0 if allowed else 0.0,
            static_score,
            0.0,
            score,
        )

    def route_pair(
        self, left: RoutePlan, right: RoutePlan, data: ProblemData
    ) -> ContextCompatibilityFeatures:
        return self.route_pair_from_values(
            route_service_nodes(left),
            route_service_nodes(right),
            left.passenger_count,
            right.passenger_count,
            left.base_airport,
            right.base_airport,
            data,
        )


@dataclass(frozen=True)
class StaticRelatednessRanker:
    model: StaticRelatednessModel

    @property
    def name(self) -> str:
        return self.model.name

    def features(
        self, left: RoutePlan, right: RoutePlan, data: ProblemData
    ) -> CandidateFeatures:
        base = RawDistanceRanker().features(left, right, data)
        values = self.model.route_pair_features(
            route_service_nodes(left), route_service_nodes(right)
        )
        return replace(
            base,
            distance_relatedness=values.distance,
            consensus_relatedness=values.consensus,
            airport_relatedness=values.airport,
            fuel_relatedness=values.fuel,
            capacity_relatedness=values.capacity,
            static_relatedness_score=values.score,
            combined_candidate_score=values.score,
        )

    def rank_key(
        self,
        anchor: RoutePlan,
        other: RoutePlan,
        anchor_index: int,
        other_index: int,
        data: ProblemData,
    ) -> tuple[object, ...]:
        features = self.features(anchor, other, data)
        return (
            -float(features.static_relatedness_score or 0.0),
            features.minimum_distance_km,
            route_signature(other),
            other_index,
        )

    def pair_key(
        self,
        left: RoutePlan,
        right: RoutePlan,
        left_index: int,
        right_index: int,
        data: ProblemData,
    ) -> tuple[object, ...]:
        features = self.features(left, right, data)
        return (
            -float(features.static_relatedness_score or 0.0),
            features.minimum_distance_km,
            route_signature(left),
            route_signature(right),
            left_index,
            right_index,
        )


@dataclass(frozen=True)
class ContextRelatednessRanker:
    static_ranker: StaticRelatednessRanker
    context: ContextCompatibility
    context_weight: float = 0.5

    def __post_init__(self) -> None:
        if not 0.0 <= self.context_weight <= 1.0:
            raise ValueError("context_weight must lie in [0, 1]")

    @property
    def name(self) -> str:
        return f"{self.static_ranker.name}_context"

    def features(
        self, left: RoutePlan, right: RoutePlan, data: ProblemData
    ) -> CandidateFeatures:
        static = self.static_ranker.features(left, right, data)
        context = self.context.route_pair(left, right, data)
        combined = (
            (1.0 - self.context_weight) * float(static.static_relatedness_score or 0.0)
            + self.context_weight * context.score
        )
        return replace(
            static,
            context_capacity_score=context.capacity_fill,
            context_stop_score=context.stop_score,
            context_airport_score=context.airport_compatible,
            context_relatedness_score=context.score,
            combined_candidate_score=combined,
        )

    def rank_key(
        self,
        anchor: RoutePlan,
        other: RoutePlan,
        anchor_index: int,
        other_index: int,
        data: ProblemData,
    ) -> tuple[object, ...]:
        features = self.features(anchor, other, data)
        return (
            -float(features.combined_candidate_score or 0.0),
            features.minimum_distance_km,
            route_signature(other),
            other_index,
        )

    def pair_key(
        self,
        left: RoutePlan,
        right: RoutePlan,
        left_index: int,
        right_index: int,
        data: ProblemData,
    ) -> tuple[object, ...]:
        features = self.features(left, right, data)
        return (
            -float(features.combined_candidate_score or 0.0),
            features.minimum_distance_km,
            route_signature(left),
            route_signature(right),
            left_index,
            right_index,
        )
