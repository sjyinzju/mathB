from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Mapping, Protocol, Sequence

from ..io_utils import read_csv
from .clustering import ClusterResult, DistanceMatrix
from .data import ProblemData
from .models import RoutePlan


FuelSignature = tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class CandidateFeatures:
    same_cluster_fraction: float
    minimum_distance_km: float
    airport_profile_gap_km: float
    fuel_signature_gap: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class CandidateRanker(Protocol):
    name: str

    def rank_key(
        self,
        anchor: RoutePlan,
        other: RoutePlan,
        anchor_index: int,
        other_index: int,
        data: ProblemData,
    ) -> tuple[object, ...]: ...

    def pair_key(
        self,
        left: RoutePlan,
        right: RoutePlan,
        left_index: int,
        right_index: int,
        data: ProblemData,
    ) -> tuple[object, ...]: ...

    def features(
        self, left: RoutePlan, right: RoutePlan, data: ProblemData
    ) -> CandidateFeatures: ...


def route_service_nodes(route: RoutePlan) -> tuple[str, ...]:
    if route.service_facilities:
        return tuple(dict.fromkeys(route.service_facilities))
    return tuple(dict.fromkeys(item.destination_id for item in route.assignments))


def route_signature(route: RoutePlan) -> str:
    services = "+".join(sorted(route_service_nodes(route)))
    people = "+".join(sorted(item.person_id for item in route.assignments))
    return f"{route.base_airport}|{services}|{route.passenger_count}|{people}"


def load_fuel_signatures(path: Path | str) -> dict[str, FuelSignature]:
    rows = read_csv(path)
    grouped: dict[str, list[tuple[tuple[str, str], tuple[int, int]]]] = {}
    for row in rows:
        grouped.setdefault(row["facility"], []).append(
            (
                (row["aircraft_type"], row["airport"]),
                (
                    int(row["minimum_sea_stops_with_refuel_allowed"]),
                    int(row["refuel_required_for_closed_route"]),
                ),
            )
        )
    return {
        facility: tuple(value for _, value in sorted(values))
        for facility, values in grouped.items()
    }


def fuel_signature_gap(left: FuelSignature, right: FuelSignature) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("fuel signatures must be non-empty and aligned")
    return mean(
        (abs(left_stops - right_stops) / 4 + int(left_refuel != right_refuel)) / 2
        for (left_stops, left_refuel), (right_stops, right_refuel) in zip(left, right)
    )


@dataclass(frozen=True)
class RelatednessModel:
    clusters: ClusterResult
    matrix: DistanceMatrix
    airports: tuple[str, ...]
    fuel_signatures: Mapping[str, FuelSignature] | None = None

    @property
    def label_by_facility(self) -> dict[str, int]:
        return self.clusters.label_by_facility

    def relatedness(self, left: str, right: str) -> float:
        return float(self.label_by_facility[left] == self.label_by_facility[right])

    def facility_pair_features(self, left: str, right: str) -> CandidateFeatures:
        fuel_gap = None
        if self.fuel_signatures is not None:
            fuel_gap = fuel_signature_gap(
                self.fuel_signatures[left], self.fuel_signatures[right]
            )
        return CandidateFeatures(
            same_cluster_fraction=self.relatedness(left, right),
            minimum_distance_km=float(self.matrix[left][right]),
            airport_profile_gap_km=mean(
                abs(float(self.matrix[airport][left]) - float(self.matrix[airport][right]))
                for airport in self.airports
            ),
            fuel_signature_gap=fuel_gap,
        )

    def route_pair_features(
        self, left_services: Sequence[str], right_services: Sequence[str]
    ) -> CandidateFeatures:
        labels = self.label_by_facility
        pairs: list[CandidateFeatures] = []
        for left in left_services:
            for right in right_services:
                fuel_gap = None
                if self.fuel_signatures is not None:
                    fuel_gap = fuel_signature_gap(
                        self.fuel_signatures[left], self.fuel_signatures[right]
                    )
                pairs.append(
                    CandidateFeatures(
                        same_cluster_fraction=float(labels[left] == labels[right]),
                        minimum_distance_km=float(self.matrix[left][right]),
                        airport_profile_gap_km=mean(
                            abs(
                                float(self.matrix[airport][left])
                                - float(self.matrix[airport][right])
                            )
                            for airport in self.airports
                        ),
                        fuel_signature_gap=fuel_gap,
                    )
                )
        if not pairs:
            raise ValueError("route relatedness requires non-empty service sets")
        fuel_values = [
            value.fuel_signature_gap
            for value in pairs
            if value.fuel_signature_gap is not None
        ]
        return CandidateFeatures(
            same_cluster_fraction=mean(value.same_cluster_fraction for value in pairs),
            minimum_distance_km=min(value.minimum_distance_km for value in pairs),
            airport_profile_gap_km=mean(value.airport_profile_gap_km for value in pairs),
            fuel_signature_gap=mean(fuel_values) if fuel_values else None,
        )


@dataclass(frozen=True)
class RawDistanceRanker:
    fuel_signatures: Mapping[str, FuelSignature] | None = None
    name: str = "raw_distance"

    def features(
        self, left: RoutePlan, right: RoutePlan, data: ProblemData
    ) -> CandidateFeatures:
        left_nodes = route_service_nodes(left)
        right_nodes = route_service_nodes(right)
        pairs = [(a, b) for a in left_nodes for b in right_nodes]
        fuel_values = (
            [
                fuel_signature_gap(self.fuel_signatures[a], self.fuel_signatures[b])
                for a, b in pairs
            ]
            if self.fuel_signatures is not None
            else []
        )
        return CandidateFeatures(
            same_cluster_fraction=0.0,
            minimum_distance_km=min(float(data.matrix[a][b]) for a, b in pairs),
            airport_profile_gap_km=mean(
                mean(
                    abs(float(data.matrix[airport][a]) - float(data.matrix[airport][b]))
                    for airport in data.config.airports
                )
                for a, b in pairs
            ),
            fuel_signature_gap=mean(fuel_values) if fuel_values else None,
        )

    def rank_key(
        self,
        anchor: RoutePlan,
        other: RoutePlan,
        anchor_index: int,
        other_index: int,
        data: ProblemData,
    ) -> tuple[object, ...]:
        return (self.features(anchor, other, data).minimum_distance_km, other_index)

    def pair_key(
        self,
        left: RoutePlan,
        right: RoutePlan,
        left_index: int,
        right_index: int,
        data: ProblemData,
    ) -> tuple[object, ...]:
        return (
            self.features(left, right, data).minimum_distance_km,
            left_index,
            right_index,
        )


@dataclass(frozen=True)
class ClusterCandidateRanker:
    model: RelatednessModel

    @property
    def name(self) -> str:
        return f"cluster_{self.model.clusters.method}_k{self.model.clusters.k}"

    def features(
        self, left: RoutePlan, right: RoutePlan, data: ProblemData
    ) -> CandidateFeatures:
        return self.model.route_pair_features(
            route_service_nodes(left), route_service_nodes(right)
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
            -features.same_cluster_fraction,
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
            -features.same_cluster_fraction,
            features.minimum_distance_km,
            route_signature(left),
            route_signature(right),
            left_index,
            right_index,
        )


def empirical_percentile_component(
    facilities: Sequence[str], values: Mapping[tuple[str, str], float]
) -> dict[str, dict[str, float]]:
    """Convert a symmetric pair component to an empirical percentile matrix."""

    pairs = [
        (left, right)
        for left_index, left in enumerate(facilities)
        for right in facilities[left_index + 1 :]
    ]
    ranked = sorted((float(values[tuple(sorted(pair))]), pair) for pair in pairs)
    percentiles: dict[tuple[str, str], float] = {}
    index = 0
    while index < len(ranked):
        end = index + 1
        while end < len(ranked) and ranked[end][0] == ranked[index][0]:
            end += 1
        average_rank = (index + end - 1) / 2
        percentile = average_rank / (len(ranked) - 1) if len(ranked) > 1 else 0.0
        for _, pair in ranked[index:end]:
            percentiles[tuple(sorted(pair))] = percentile
        index = end
    matrix = {facility: {facility: 0.0} for facility in facilities}
    for (left, right), percentile in percentiles.items():
        matrix[left][right] = percentile
        matrix[right][left] = percentile
    return matrix


def composite_dissimilarity(
    facilities: Sequence[str],
    components: Mapping[str, Mapping[str, Mapping[str, float]]],
    weights: Mapping[str, float],
) -> dict[str, dict[str, float]]:
    if set(components) != set(weights):
        raise ValueError("component and weight names must match")
    if any(weight < 0 for weight in weights.values()):
        raise ValueError("weights must be non-negative")
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("weights must sum to one")
    return {
        left: {
            right: sum(
                weights[name] * float(component[left][right])
                for name, component in components.items()
            )
            for right in facilities
        }
        for left in facilities
    }
