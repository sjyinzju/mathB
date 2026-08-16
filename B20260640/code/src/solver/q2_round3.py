from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Iterable, Sequence

from .cache import SolverCache
from .data import ProblemData
from .evaluator import evaluate_route
from .models import RoutePlan, Solution
from .q2_lns import Q2LnsConfig, Q2LocalRepair, exact_q2_local_repair


ROUND3_SCHEMA_VERSION = 2


def _route_facilities(route: RoutePlan, data: ProblemData) -> frozenset[str]:
    nodes = set(route.service_facilities)
    for assignment in route.assignments:
        if assignment.origin_id in data.config.facilities:
            nodes.add(assignment.origin_id)
        if assignment.destination_id in data.config.facilities:
            nodes.add(assignment.destination_id)
    return frozenset(nodes)


def _land_count(route: RoutePlan) -> int:
    return sum(
        assignment.origin_id == "LAND" or assignment.destination_id == "LAND"
        for assignment in route.assignments
    )


def _direction_counts(route: RoutePlan, data: ProblemData) -> tuple[int, int, int]:
    airports = set(data.config.airports)
    outbound = inbound = shuttle = 0
    for assignment in route.assignments:
        origin_airport = assignment.origin_id in airports or assignment.origin_id == "LAND"
        destination_airport = (
            assignment.destination_id in airports or assignment.destination_id == "LAND"
        )
        if origin_airport and not destination_airport:
            outbound += 1
        elif destination_airport and not origin_airport:
            inbound += 1
        else:
            shuttle += 1
    return outbound, inbound, shuttle


def q2_basin_fingerprint(solution: Solution) -> str:
    """Stable structural basin identity, independent of route numbering."""
    routes = []
    for route in solution.routes:
        routes.append(
            {
                "airport": route.base_airport,
                "aircraft": route.aircraft_type,
                "service": list(route.service_facilities),
                "people": sorted(assignment.person_id for assignment in route.assignments),
                "od": sorted(
                    (assignment.origin_id, assignment.destination_id)
                    for assignment in route.assignments
                ),
            }
        )
    payload = json.dumps(
        sorted(routes, key=lambda row: json.dumps(row, sort_keys=True)),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def audit_q2_solution(
    solution: Solution,
    data: ProblemData,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Return complete per-route audit and directed source-target compatibility."""
    route_rows: list[dict[str, object]] = []
    evaluations = [evaluate_route(route, matrix=data.matrix, config=data.config) for route in solution.routes]
    facilities = [_route_facilities(route, data) for route in solution.routes]
    residual_profiles: list[list[int]] = []
    for route, evaluation in zip(solution.routes, evaluations):
        capacity = data.config.aircraft_types[route.aircraft_type].seats
        residual = [capacity - leg.departure_load for leg in evaluation.legs]
        residual_profiles.append(residual)
        outbound, inbound, shuttle = _direction_counts(route, data)
        land = _land_count(route)
        route_rows.append(
            {
                "route_index": len(route_rows),
                "aircraft_time": evaluation.total_aircraft_time_minutes,
                "aircraft_type": route.aircraft_type,
                "base_airport": route.base_airport,
                "passenger_count": route.passenger_count,
                "service_sequence": "|".join(route.service_facilities),
                "offshore_landing_count": len(route.service_facilities),
                "utilization": evaluation.seat_utilization,
                "per_leg_load": "|".join(str(leg.departure_load) for leg in evaluation.legs),
                "min_residual_seats": min(residual, default=capacity),
                "mean_residual_seats": mean(residual) if residual else capacity,
                "outbound_passenger_count": outbound,
                "inbound_passenger_count": inbound,
                "shuttle_passenger_count": shuttle,
                "land_count": land,
                "land_fraction": land / max(1, route.passenger_count),
                "fixed_airport_count": route.passenger_count - land,
            }
        )

    pair_rows: list[dict[str, object]] = []
    for source_index, source in enumerate(solution.routes):
        source_facilities = facilities[source_index]
        source_od = {
            (item.origin_id, item.destination_id) for item in source.assignments
        }
        for target_index, target in enumerate(solution.routes):
            if source_index == target_index:
                continue
            target_facilities = facilities[target_index]
            target_od = {
                (item.origin_id, item.destination_id) for item in target.assignments
            }
            distances = [
                data.matrix[left][right]
                for left in source_facilities
                for right in target_facilities
            ]
            target_residual = residual_profiles[target_index]
            shared = len(source_facilities & target_facilities)
            airport_same = source.base_airport == target.base_airport
            source_land = _land_count(source)
            target_land = _land_count(target)
            pair_rows.append(
                {
                    "source_route": source_index,
                    "target_route": target_index,
                    "facility_overlap": shared,
                    "od_overlap": len(source_od & target_od),
                    "minimum_facility_distance_km": min(distances, default=math.inf),
                    "airport_compatible": airport_same or source_land > 0 or target_land > 0,
                    "same_base_airport": airport_same,
                    "target_residual_sum": sum(target_residual),
                    "target_residual_min": min(target_residual, default=0),
                    "residual_seat_complementarity": min(
                        source.passenger_count, sum(max(0, value) for value in target_residual)
                    ),
                    "land_reassignment_compatible": source_land > 0 or target_land > 0,
                    "shuttle_interaction": any(
                        item.origin_id in source_facilities
                        and item.destination_id in target_facilities
                        or item.origin_id in target_facilities
                        and item.destination_id in source_facilities
                        for item in (*source.assignments, *target.assignments)
                    ),
                }
            )
    return route_rows, pair_rows


def absorption_potential_ranking(
    route_rows: Sequence[dict[str, object]],
    pair_rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Outcome-free prioritization; exact repair remains the decision maker."""
    by_source: dict[int, list[dict[str, object]]] = {}
    for row in pair_rows:
        by_source.setdefault(int(row["source_route"]), []).append(row)

    def percentile(values: dict[int, float], key: int, higher: bool = True) -> float:
        ordered = sorted(values, key=lambda item: (values[item], item), reverse=higher)
        return 1.0 - ordered.index(key) / max(1, len(ordered) - 1)

    raw: dict[int, dict[str, float]] = {}
    for row in route_rows:
        index = int(row["route_index"])
        pairs = by_source.get(index, [])
        compatible = [item for item in pairs if item["airport_compatible"]]
        raw[index] = {
            "low_passengers": -float(row["passenger_count"]),
            "low_utilization": -float(row["utilization"]),
            "source_time": float(row["aircraft_time"]),
            "land_flexibility": float(row["land_fraction"]),
            "compatible_neighbors": float(len(compatible)),
            "neighbor_slack": float(sum(float(item["target_residual_sum"]) for item in compatible)),
            "shared_facilities": float(sum(float(item["facility_overlap"]) for item in compatible)),
            "geometry": -min(
                (float(item["minimum_facility_distance_km"]) for item in compatible),
                default=1.0e9,
            ),
        }
    ranked: list[dict[str, object]] = []
    for row in route_rows:
        index = int(row["route_index"])
        components = {
            name: percentile({idx: values[name] for idx, values in raw.items()}, index)
            for name in raw[index]
        }
        score = mean(components.values())
        ranked.append(
            {
                **row,
                "absorption_score": round(score, 9),
                "absorption_components": json.dumps(components, sort_keys=True),
                "compatible_neighbor_count": int(raw[index]["compatible_neighbors"]),
                "neighbor_slack_sum": raw[index]["neighbor_slack"],
            }
        )
    ranked.sort(key=lambda row: (-float(row["absorption_score"]), int(row["route_index"])))
    for rank, row in enumerate(ranked, 1):
        row["absorption_rank"] = rank
    return ranked


def select_absorption_neighborhood(
    source_index: int,
    pair_rows: Sequence[dict[str, object]],
    *,
    route_count: int,
) -> tuple[int, ...]:
    if route_count < 2:
        raise ValueError("route_count must include source and at least one target")
    candidates = [row for row in pair_rows if int(row["source_route"]) == source_index]
    candidates.sort(
        key=lambda row: (
            -int(bool(row["airport_compatible"])),
            -int(row["facility_overlap"]),
            -int(row["od_overlap"]),
            -float(row["residual_seat_complementarity"]),
            float(row["minimum_facility_distance_km"]),
            int(row["target_route"]),
        )
    )
    targets = [int(row["target_route"]) for row in candidates[: route_count - 1]]
    return tuple([source_index, *targets])


@dataclass(frozen=True)
class PromisingLocalMaster:
    identity: str
    source_routes: tuple[int, ...]
    candidate_pool_hash: str | None
    initial_incumbent: int | None
    initial_bound: float | None
    initial_gap: float | None
    status: int | None
    reason: str
    attempt: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PromisingLocalMasterQueue:
    def __init__(self) -> None:
        self._entries: dict[str, PromisingLocalMaster] = {}

    @property
    def entries(self) -> tuple[PromisingLocalMaster, ...]:
        return tuple(
            sorted(
                self._entries.values(),
                key=lambda item: (
                    -(item.initial_gap or 0.0),
                    item.initial_incumbent or math.inf,
                    item.identity,
                ),
            )
        )

    def add_from_repair(self, repair: Q2LocalRepair) -> bool:
        diagnostics = repair.diagnostics
        gap = diagnostics.get("primary_mip_gap")
        route_ejected = bool(diagnostics.get("route_ejected"))
        selected_new = int(diagnostics.get("selected_new_candidates", 0))
        if gap is None or (float(gap) <= 0.01 and not route_ejected and selected_new == 0):
            return False
        routes = tuple(int(value) for value in diagnostics.get("destroyed_routes", []))
        pool_hash = diagnostics.get("candidate_pool_hash")
        identity = hashlib.sha256(
            repr((routes, pool_hash)).encode("utf-8")
        ).hexdigest()[:24]
        reason = "+".join(
            name
            for name, active in (
                ("restricted_gap", float(gap) > 0.01),
                ("near_elimination", route_ejected),
                ("new_columns", selected_new > 0),
            )
            if active
        )
        self._entries[identity] = PromisingLocalMaster(
            identity=identity,
            source_routes=routes,
            candidate_pool_hash=str(pool_hash) if pool_hash else None,
            initial_incumbent=(
                int(diagnostics["after_aircraft_minutes"])
                if diagnostics.get("after_aircraft_minutes") is not None
                else None
            ),
            initial_bound=float(diagnostics["primary_dual_bound"])
            if diagnostics.get("primary_dual_bound") is not None
            else None,
            initial_gap=float(gap),
            status=int(diagnostics["primary_status"])
            if diagnostics.get("primary_status") is not None
            else None,
            reason=reason,
        )
        return True

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": ROUND3_SCHEMA_VERSION,
                    "entries": [entry.to_dict() for entry in self.entries],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def deepen_promising_master(
    solution: Solution,
    data: ProblemData,
    entry: PromisingLocalMaster,
    *,
    cache: SolverCache,
    base_config: Q2LnsConfig,
    candidate_budget: int,
    primary_seconds: float,
) -> Q2LocalRepair:
    config = Q2LnsConfig(
        **{
            **base_config.__dict__,
            "candidate_sequence_budget": max(
                candidate_budget, base_config.candidate_sequence_budget
            ),
            "local_primary_seconds": max(primary_seconds, base_config.local_primary_seconds),
        }
    )
    return exact_q2_local_repair(
        solution,
        data,
        entry.source_routes,
        cache=cache,
        config=config,
        require_primary_improvement=True,
        prioritize_four_stop=True,
        selection_seed=int(entry.identity[:8], 16),
        search_context={
            "candidate_source": "ABSORPTION",
            "deep_resolve": True,
            "promising_master_id": entry.identity,
            "schema_version": ROUND3_SCHEMA_VERSION,
        },
    )

