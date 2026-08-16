from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .data import ProblemData
from .models import RoutePlan


SimilarityMatrix = Mapping[str, Mapping[str, float]]


def route_service_nodes(route: RoutePlan) -> tuple[str, ...]:
    if route.service_facilities:
        return tuple(dict.fromkeys(route.service_facilities))
    return tuple(dict.fromkeys(item.destination_id for item in route.assignments))


def raw_route_distance(left: RoutePlan, right: RoutePlan, data: ProblemData) -> float:
    """Phase-2 distance control: direct facility distance, without penalties."""

    return min(
        data.matrix[left_node][right_node]
        for left_node in route_service_nodes(left)
        for right_node in route_service_nodes(right)
    )


@dataclass(frozen=True)
class FrozenConsensus:
    facilities: tuple[str, ...]
    matrix: dict[str, dict[str, float]]

    @classmethod
    def from_pair_csv(
        cls,
        path: str | Path,
        facilities: Sequence[str],
    ) -> "FrozenConsensus":
        facility_tuple = tuple(facilities)
        matrix = {
            left: {right: (1.0 if left == right else float("nan")) for right in facility_tuple}
            for left in facility_tuple
        }
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                left = row["left"]
                right = row["right"]
                value = float(row["consensus"])
                if left not in matrix or right not in matrix:
                    raise ValueError(f"unknown consensus facility pair: {left}, {right}")
                if not 0.0 <= value <= 1.0:
                    raise ValueError("consensus values must lie in [0, 1]")
                matrix[left][right] = value
                matrix[right][left] = value
        missing = [
            (left, right)
            for left in facility_tuple
            for right in facility_tuple
            if matrix[left][right] != matrix[left][right]
        ]
        if missing:
            raise ValueError(f"consensus matrix is incomplete; first missing pair={missing[0]}")
        return cls(facility_tuple, matrix)

    def route_similarity(self, left: RoutePlan, right: RoutePlan) -> float:
        return max(
            self.matrix[left_node][right_node]
            for left_node in route_service_nodes(left)
            for right_node in route_service_nodes(right)
        )


def _midranks(values: Mapping[int, float], *, reverse: bool = False) -> dict[int, float]:
    ordered = sorted(values.items(), key=lambda item: ((-item[1] if reverse else item[1]), item[0]))
    result: dict[int, float] = {}
    position = 0
    while position < len(ordered):
        end = position + 1
        while end < len(ordered) and ordered[end][1] == ordered[position][1]:
            end += 1
        rank = 0.5 * (position + end - 1)
        for index, _ in ordered[position:end]:
            result[index] = rank
        position = end
    denominator = max(1, len(ordered) - 1)
    return {index: rank / denominator for index, rank in result.items()}


def rank_related_routes(
    anchor: RoutePlan,
    candidate_indices: Sequence[int],
    routes: Sequence[RoutePlan],
    data: ProblemData,
    *,
    mode: str,
    consensus: FrozenConsensus | None = None,
) -> list[int]:
    """Rank route neighbours; scores guide selection but never imply legality."""

    distances = {
        index: raw_route_distance(anchor, routes[index], data)
        for index in candidate_indices
    }
    if mode == "distance":
        return sorted(candidate_indices, key=lambda index: (distances[index], index))
    if mode != "distance_consensus":
        raise ValueError(f"unsupported relatedness mode: {mode}")
    if consensus is None:
        raise ValueError("distance_consensus mode requires a frozen consensus matrix")
    similarities = {
        index: consensus.route_similarity(anchor, routes[index])
        for index in candidate_indices
    }
    distance_ranks = _midranks(distances)
    consensus_ranks = _midranks(similarities, reverse=True)
    return sorted(
        candidate_indices,
        key=lambda index: (
            distance_ranks[index] + consensus_ranks[index],
            distances[index],
            -similarities[index],
            index,
        ),
    )


CONTEXT_COMPONENTS = ("geometry", "capacity", "ejection", "airport", "route_state")


@dataclass(frozen=True)
class RepairCandidateSpec:
    base_airport: str
    aircraft_type: str
    service_order: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str, tuple[str, ...]]:
        return self.base_airport, self.aircraft_type, self.service_order


@dataclass(frozen=True)
class ContextRepairFeatures:
    geometry_km: float
    capacity_fill: float
    minimum_slack: float
    ejection_potential: float
    airport_compatibility: float
    route_state_similarity: float
    rank_score: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def _candidate_features(
    candidate: RepairCandidateSpec,
    groups: Mapping[tuple[str, str], Sequence[object]],
    destroyed_routes: Sequence[RoutePlan],
    data: ProblemData,
) -> ContextRepairFeatures:
    capacity = data.config.aircraft_types[candidate.aircraft_type].seats
    compatible_demand = sum(
        len(passengers)
        for (origin, destination), passengers in groups.items()
        if destination in candidate.service_order
        and (origin == "LAND" or origin == candidate.base_airport)
    )
    destination_demand = sum(
        len(passengers)
        for (_, destination), passengers in groups.items()
        if destination in candidate.service_order
    )
    effective_load = min(capacity, compatible_demand)
    capacity_fill = effective_load / capacity
    minimum_slack = (capacity - effective_load) / capacity
    locations = (candidate.base_airport, *candidate.service_order, candidate.base_airport)
    geometry = sum(
        data.matrix[left][right] for left, right in zip(locations, locations[1:])
    )
    matched_sources = sum(
        route.base_airport == candidate.base_airport
        and set(route_service_nodes(route)) <= set(candidate.service_order)
        for route in destroyed_routes
    )
    ejection = (
        matched_sources / max(1, len(destroyed_routes))
        + (len(candidate.service_order) - 1) / max(1, data.config.max_sea_landings - 1)
    ) / 2.0
    airport = compatible_demand / max(1, destination_demand)
    route_state = max(
        (
            len(set(candidate.service_order) & set(route_service_nodes(route)))
            / len(set(candidate.service_order) | set(route_service_nodes(route)))
            * (1.0 if route.base_airport == candidate.base_airport else 0.5)
            * (1.0 if route.aircraft_type == candidate.aircraft_type else 0.8)
            for route in destroyed_routes
        ),
        default=0.0,
    )
    return ContextRepairFeatures(
        geometry_km=float(geometry),
        capacity_fill=float(capacity_fill),
        minimum_slack=float(minimum_slack),
        ejection_potential=float(ejection),
        airport_compatibility=float(airport),
        route_state_similarity=float(route_state),
        rank_score=0.0,
    )


def rank_context_repair_candidates(
    candidates: Sequence[RepairCandidateSpec],
    groups: Mapping[tuple[str, str], Sequence[object]],
    destroyed_routes: Sequence[RoutePlan],
    data: ProblemData,
    *,
    components: Sequence[str] = CONTEXT_COMPONENTS,
) -> tuple[list[RepairCandidateSpec], dict[tuple[str, str, tuple[str, ...]], ContextRepairFeatures]]:
    """Rank cheap repair candidates before any augmentation or exact evaluation."""

    enabled = tuple(components)
    if not enabled or not set(enabled) <= set(CONTEXT_COMPONENTS):
        raise ValueError(f"context components must be a non-empty subset of {CONTEXT_COMPONENTS}")
    base_features = {
        candidate.key: _candidate_features(candidate, groups, destroyed_routes, data)
        for candidate in candidates
    }
    component_values: dict[str, dict[int, float]] = {
        "geometry": {
            index: base_features[candidate.key].geometry_km
            for index, candidate in enumerate(candidates)
        },
        "capacity": {
            index: base_features[candidate.key].capacity_fill
            for index, candidate in enumerate(candidates)
        },
        "ejection": {
            index: base_features[candidate.key].ejection_potential
            for index, candidate in enumerate(candidates)
        },
        "airport": {
            index: base_features[candidate.key].airport_compatibility
            for index, candidate in enumerate(candidates)
        },
        "route_state": {
            index: base_features[candidate.key].route_state_similarity
            for index, candidate in enumerate(candidates)
        },
    }
    ranks = {
        name: _midranks(values, reverse=name != "geometry")
        for name, values in component_values.items()
        if name in enabled
    }
    scores = {
        index: sum(values[index] for values in ranks.values()) / len(ranks)
        for index in range(len(candidates))
    }
    features = {
        candidate.key: ContextRepairFeatures(
            **{
                key: value
                for key, value in base_features[candidate.key].to_dict().items()
                if key != "rank_score"
            },
            rank_score=scores[index],
        )
        for index, candidate in enumerate(candidates)
    }
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            features[candidate.key].rank_score,
            features[candidate.key].geometry_km,
            candidate.key,
        ),
    )
    return ordered, features
