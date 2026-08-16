from __future__ import annotations

import hashlib
import json
import random
import time
from collections import Counter, defaultdict, deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ..io_utils import write_json
from .data import ProblemData
from .q3 import (
    Q3Flight,
    Q3Person,
    Q3Variant,
    _assignment_for_person,
    _flight_accepts_person,
    optimize_fixed_flight_assignments,
    schedule_metrics,
    stage1_key,
    stage2_key,
)
from .q3_closure_p2 import (
    cross_day_flexible_descent,
    generalized_multiflight_ruin_recreate,
    optional_feasibility_dossiers,
)
from .q3_pro import (
    ElitePool,
    long_horizon_alns,
    path_relink_and_recombine,
    solution_distance,
    solution_signature,
)


@dataclass(frozen=True)
class PortfolioConfig:
    config_id: str
    seed: int
    restart_threshold: int
    normal_group_min: int
    normal_group_max: int
    heavy_group_min: int
    heavy_group_max: int
    cross_day_trials: int
    route_limit: int
    heavy_route_limit: int
    combination_budget: int
    heavy_combination_budget: int
    reaction_factor: float
    operator_profile: str
    assignment_time_limit: float

    def operator_weights(self) -> dict[str, float]:
        profiles = {
            "crossday": {
                "cross_day": 3.5,
                "random_related": 1.1,
                "route_polish": 1.0,
                "aircraft_chain": 0.8,
            },
            "diverse": {
                "cross_day": 1.6,
                "random_related": 2.6,
                "conflict_graph": 1.8,
                "bottleneck_day": 1.6,
            },
            "chain": {
                "cross_day": 1.4,
                "aircraft_chain": 3.0,
                "exact_lns": 1.2,
                "high_cost": 1.4,
            },
            "polish": {
                "cross_day": 1.5,
                "route_polish": 3.0,
                "low_utilization": 1.6,
                "high_cost": 1.8,
            },
        }
        return profiles[self.operator_profile]


def parameter_grid(master_seed: int, count: int = 20) -> list[PortfolioConfig]:
    """Deterministic heterogeneous portfolio, intentionally not one long basin."""

    profiles = ("crossday", "diverse", "chain", "polish")
    configs: list[PortfolioConfig] = []
    for index in range(count):
        configs.append(
            PortfolioConfig(
                config_id=f"cfg-{index:02d}",
                seed=master_seed + 1009 * index,
                restart_threshold=(30, 45, 60, 80, 100)[index % 5],
                normal_group_min=(2, 3, 3, 4)[index % 4],
                normal_group_max=(5, 6, 7, 8)[index % 4],
                heavy_group_min=(4, 5, 6)[index % 3],
                heavy_group_max=(8, 10, 12)[index % 3],
                cross_day_trials=(4, 6, 8, 10, 12)[index % 5],
                route_limit=(100, 140, 180, 220)[index % 4],
                heavy_route_limit=(220, 280, 340, 420)[index % 4],
                combination_budget=(3, 5, 8, 12)[index % 4],
                heavy_combination_budget=(12, 20, 32, 48)[index % 4],
                reaction_factor=(0.08, 0.12, 0.18, 0.25)[index % 4],
                operator_profile=profiles[index % len(profiles)],
                assignment_time_limit=(3.0, 4.0, 5.0, 6.0)[index % 4],
            )
        )
    return configs


def _leg_occupancy(flight: Q3Flight) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    stops = flight.variant.source.route.stops
    for leg in range(len(stops) - 1):
        occupants = sorted(
            person_id
            for person_id, (pickup, delivery) in flight.assignment_intervals.items()
            if pickup <= leg < delivery
        )
        rows.append(
            {
                "leg": leg,
                "from": stops[leg].facility_id,
                "to": stops[leg + 1].facility_id,
                "load": len(occupants),
                "capacity": flight.variant.capacity,
                "slack": flight.variant.capacity - len(occupants),
                "occupants": occupants,
            }
        )
    return rows


def critical_leg_graph(
    flights: Sequence[Q3Flight], people: dict[str, Q3Person]
) -> dict[str, object]:
    """Build the exact passenger-to-seat-leg occupancy graph."""

    hubs: list[dict[str, object]] = []
    passenger_degree: Counter[str] = Counter()
    for flight_index, flight in enumerate(flights):
        for row in _leg_occupancy(flight):
            if int(row["slack"]) > 0:
                continue
            occupants = list(row["occupants"])
            for person_id in occupants:
                passenger_degree[person_id] += len(occupants) - 1
            hubs.append(
                {
                    "flight_index": flight_index,
                    "aircraft_id": flight.aircraft_id,
                    "day": flight.start // 1440,
                    **row,
                }
            )
    hubs.sort(key=lambda row: (-int(row["load"]), int(row["flight_index"]), int(row["leg"])))
    return {
        "critical_leg_count": len(hubs),
        "critical_legs": hubs,
        "passenger_degree": dict(passenger_degree.most_common()),
        "mandatory_hub_occupants": sum(
            people[pid].mandatory
            for row in hubs
            for pid in row["occupants"]
            if pid in people
        ),
    }


def optional_rescue_dossier_v2(
    flights: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
) -> dict[str, object]:
    """Dynamic blocker profile with critical legs and relocation alternatives."""

    base = optional_feasibility_dossiers(flights, people, variants_by_od, data)
    records: list[dict[str, object]] = []
    for basic in base:
        person = people[str(basic["person_id"])]
        compatible: list[dict[str, object]] = []
        blocker_people: set[str] = set()
        for flight_index in basic["time_compatible_flights"]:
            flight = flights[int(flight_index)]
            assignment = _assignment_for_person(person, flight.variant, data.config)
            if assignment is None:
                continue
            pickup, delivery = assignment
            critical = []
            for row in _leg_occupancy(flight):
                leg = int(row["leg"])
                if pickup <= leg < delivery and int(row["slack"]) <= 0:
                    critical.append(row)
                    blocker_people.update(str(pid) for pid in row["occupants"])
            compatible.append(
                {
                    "flight_index": int(flight_index),
                    "aircraft_id": flight.aircraft_id,
                    "day": flight.start // 1440,
                    "assignment_interval": [pickup, delivery],
                    "critical_legs": critical,
                }
            )
        alternatives: dict[str, list[dict[str, object]]] = {}
        for blocker_id in sorted(blocker_people):
            blocker = people[blocker_id]
            choices = []
            for index, candidate in enumerate(flights):
                if blocker_id in candidate.person_ids:
                    continue
                interval = _flight_accepts_person(candidate, blocker, people, data.config)
                if interval is not None:
                    choices.append(
                        {
                            "flight_index": index,
                            "aircraft_id": candidate.aircraft_id,
                            "day": candidate.start // 1440,
                            "interval": list(interval),
                        }
                    )
            alternatives[blocker_id] = choices[:20]
        route_bases = Counter(variant.base_airport for variant in variants_by_od[person.od])
        route_types = Counter(variant.aircraft_type for variant in variants_by_od[person.od])
        records.append(
            {
                **basic,
                "compatible_incumbent_detail": compatible,
                "critical_blocker_passengers": sorted(blocker_people),
                "blocker_alternative_flights": alternatives,
                "compatible_bases": dict(route_bases),
                "compatible_aircraft_types": dict(route_types),
                "base_reassignment_relevant": "LAND" in person.od,
            }
        )
    return {
        "unserved_count": len(records),
        "records": records,
        "critical_leg_graph": critical_leg_graph(flights, people),
    }


def find_ejection_chains(
    dossier: dict[str, object], *, maximum_depth: int = 8, beam_width: int = 24
) -> list[dict[str, object]]:
    """Enumerate fixed-flight seat ejection chains from the blocker dossier.

    This is a graph search and a diagnostic candidate generator. Final chain
    feasibility is always delegated to the exact fixed-flight assignment MILP.
    """

    results: list[dict[str, object]] = []
    for record in dossier.get("records", []):
        target = str(record["person_id"])
        alternatives = dict(record.get("blocker_alternative_flights", {}))
        queue: deque[tuple[str, tuple[str, ...], int]] = deque(
            (str(pid), (target, str(pid)), 1)
            for pid in record.get("critical_blocker_passengers", [])
        )
        visited: set[tuple[str, int]] = set()
        expanded = 0
        while queue and expanded < beam_width * maximum_depth:
            passenger_id, chain, depth = queue.popleft()
            state = (passenger_id, depth)
            if state in visited:
                continue
            visited.add(state)
            expanded += 1
            choices = alternatives.get(passenger_id, [])
            if choices:
                results.append(
                    {
                        "target": target,
                        "chain": list(chain),
                        "depth": depth,
                        "terminal_alternatives": choices[:beam_width],
                        "status": "candidate_requires_exact_assignment_gate",
                    }
                )
            if depth >= maximum_depth:
                continue
            # A deeper structural chain is represented explicitly even when the
            # next blocker is not known until a candidate flight is selected.
            for next_id in record.get("critical_blocker_passengers", []):
                next_id = str(next_id)
                if next_id not in chain:
                    queue.append((next_id, chain + (next_id,), depth + 1))
    results.sort(key=lambda row: (int(row["depth"]), str(row["target"]), row["chain"]))
    return results


def kempe_exchange_cycles(
    current_flight: dict[str, str],
    compatible_flights: dict[str, Sequence[str]],
    *,
    maximum_length: int = 6,
) -> list[list[str]]:
    """Find passenger exchange cycles in a flight-compatibility graph.

    The helper is independent of capacity. Callers use critical-leg occupancy
    to select passengers, then pass every proposed cycle through the exact
    leg-capacity assignment model.
    """

    by_flight: dict[str, list[str]] = defaultdict(list)
    for person_id, flight_id in current_flight.items():
        by_flight[flight_id].append(person_id)
    cycles: set[tuple[str, ...]] = set()
    for start in sorted(current_flight):
        queue: deque[tuple[str, tuple[str, ...]]] = deque([(start, (start,))])
        while queue:
            person_id, path = queue.popleft()
            if len(path) > maximum_length:
                continue
            for flight_id in compatible_flights.get(person_id, ()):
                for displaced in by_flight.get(flight_id, ()):
                    if displaced == start and len(path) >= 2:
                        rotations = [path[index:] + path[:index] for index in range(len(path))]
                        cycles.add(min(rotations))
                    elif displaced not in path:
                        queue.append((displaced, path + (displaced,)))
    return [list(cycle) for cycle in sorted(cycles, key=lambda row: (len(row), row))]


class OptionalRescueSolver:
    """Escalating target-specific structural search under an immutable cap."""

    def __init__(
        self,
        people: dict[str, Q3Person],
        variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
        data: ProblemData,
        *,
        cap: int,
        seed: int,
        assignment_time_limit: float = 12.0,
    ) -> None:
        self.people = people
        self.variants_by_od = variants_by_od
        self.data = data
        self.cap = cap
        self.seed = seed
        self.assignment_time_limit = assignment_time_limit

    def run(
        self, baseline: Sequence[Q3Flight], *, trials_per_level: int = 30
    ) -> tuple[list[Q3Flight], dict[str, object]]:
        started = time.perf_counter()
        incumbent, unserved, fixed = optimize_fixed_flight_assignments(
            baseline,
            self.people,
            self.data.config,
            time_limit_seconds=self.assignment_time_limit,
        )
        dossier_before = optional_rescue_dossier_v2(
            incumbent, self.people, self.variants_by_od, self.data
        )
        chains = find_ejection_chains(dossier_before)
        target_ids = [str(row["person_id"]) for row in dossier_before["records"]]
        levels = (
            ("L1-two-flight", 2, 2, 16, 4),
            ("L2-four-flight", 2, 4, 24, 8),
            ("L3-eight-flight", 3, 8, 40, 16),
            ("L4-twelve-flight", 4, 12, 64, 32),
        )
        operators = (
            "optional_target",
            "conflict_graph",
            "aircraft_chain",
            "bottleneck_day",
            "random_related",
        )
        rows: list[dict[str, object]] = []
        best_optional = int(schedule_metrics(incumbent, self.people)["served_optional"])
        for level_index, (level, group_min, group_max, route_limit, combo) in enumerate(levels):
            for operator_index, operator in enumerate(operators):
                before_key = stage2_key(incumbent, self.people)
                candidate, trace = generalized_multiflight_ruin_recreate(
                    incumbent,
                    self.people,
                    self.variants_by_od,
                    self.data,
                    stage=2,
                    stage1_cap=self.cap,
                    minimum_optional_served=best_optional,
                    group_min=group_min,
                    group_max=group_max,
                    maximum_trials=trials_per_level,
                    maximum_neighbors=max(16, group_max * 2),
                    route_limit=route_limit,
                    assignment_time_limit_seconds=self.assignment_time_limit,
                    target_optional_ids=target_ids,
                    operator=operator,
                    seed=self.seed + 101 * level_index + operator_index,
                    combination_budget=combo,
                    max_replacements=min(group_max, 8),
                )
                accepted = stage2_key(candidate, self.people) < before_key
                if accepted:
                    incumbent = candidate
                    best_optional = int(
                        schedule_metrics(incumbent, self.people)["served_optional"]
                    )
                    dossier = optional_rescue_dossier_v2(
                        incumbent, self.people, self.variants_by_od, self.data
                    )
                    target_ids = [str(row["person_id"]) for row in dossier["records"]]
                rows.append(
                    {
                        "level": level,
                        "operator": operator,
                        "accepted": accepted,
                        "metrics": schedule_metrics(incumbent, self.people),
                        "trace": trace,
                    }
                )
                if best_optional == sum(not person.mandatory for person in self.people.values()):
                    break
            if not target_ids:
                break
        dossier_after = optional_rescue_dossier_v2(
            incumbent, self.people, self.variants_by_od, self.data
        )
        return incumbent, {
            "cap": self.cap,
            "fixed_assignment": fixed,
            "initial_unserved": unserved,
            "dossier_before": dossier_before,
            "ejection_chain_candidates": chains,
            "levels": rows,
            "dossier_after": dossier_after,
            "runtime_seconds": round(time.perf_counter() - started, 6),
            "certificate_scope": (
                "fixed-flight assignment solves are exact; structural enumeration is finite "
                "and restricted, so failure is not a global infeasibility certificate"
            ),
        }


def _pool_add_all(target: ElitePool, source: ElitePool, label: str) -> None:
    for index, record in enumerate(source.records):
        target.add(record.flights, source=f"{label}:{index}:{record.source}", seed=record.seed)


def run_portfolio_config(
    config: PortfolioConfig,
    seeds: Sequence[tuple[str, Sequence[Q3Flight]]],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    iterations: int,
    wall_time_seconds: float,
    elite_size: int = 50,
) -> tuple[list[Q3Flight], ElitePool, dict[str, object]]:
    started = time.perf_counter()
    initial = min(stage1_key(schedule, people) for _name, schedule in seeds)
    best, pool, trace = long_horizon_alns(
        seeds,
        people,
        variants_by_od,
        data,
        iterations=iterations,
        wall_time_seconds=wall_time_seconds,
        seed=config.seed,
        restart_threshold=config.restart_threshold,
        assignment_time_limit_seconds=config.assignment_time_limit,
        elite_size=elite_size,
        operator_initial_weights=config.operator_weights(),
        normal_group_range=(config.normal_group_min, config.normal_group_max),
        heavy_group_range=(config.heavy_group_min, config.heavy_group_max),
        cross_day_trials=config.cross_day_trials,
        normal_route_limit=config.route_limit,
        heavy_route_limit=config.heavy_route_limit,
        normal_combination_budget=config.combination_budget,
        heavy_combination_budget=config.heavy_combination_budget,
        reaction_factor=config.reaction_factor,
    )
    elapsed = time.perf_counter() - started
    final = stage1_key(best, people)
    attempts = sum(
        int(row["attempts"]) for row in trace["operator_stats"].values()
    )
    accepted = sum(
        int(row["accepted"]) for row in trace["operator_stats"].values()
    )
    cross = trace["operator_stats"]["cross_day"]
    summary = {
        "config": asdict(config),
        "operator_weights": config.operator_weights(),
        "initial_key": list(initial),
        "final_key": list(final),
        "best_improvement_minutes": int(initial[0] - final[0]),
        "runtime_seconds": round(elapsed, 6),
        "improvement_per_cpu_minute": (
            float(initial[0] - final[0]) / max(elapsed / 60.0, 1e-9)
        ),
        "attempts": attempts,
        "accepted": accepted,
        "accepted_rate": accepted / max(1, attempts),
        "cross_day_attempts": int(cross["attempts"]),
        "cross_day_accepted": int(cross["accepted"]),
        "cross_day_acceptance": int(cross["accepted"]) / max(1, int(cross["attempts"])),
        "elite_size": len(pool.records),
        "elite_diversity": pool.summary()["mean_pairwise_distance"],
        "completed_iterations": trace["completed_iterations"],
        "trace": trace,
    }
    return best, pool, summary


def pricing_guided_variant_pool(
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    pricing: dict[str, object],
    *,
    per_od: int = 8,
) -> tuple[dict[tuple[str, str], tuple[Q3Variant, ...]], dict[str, object]]:
    """Materialize RMP route rankings as a primal candidate pool."""

    selected = {
        str(row["route_key"])
        for section in ("restricted_master", "priced_full_pool_master")
        for row in pricing.get(section, {}).get("details", {}).get("top_selected_routes", [])
    }
    dual_od = {
        tuple(row["od"]): float(row["dual_minutes"])
        for section in ("priced_full_pool_master", "restricted_master")
        for row in pricing.get(section, {}).get("details", {}).get("top_od_duals", [])
    }
    result: dict[tuple[str, str], tuple[Q3Variant, ...]] = {}
    imported = 0
    for od, variants in variants_by_od.items():
        ranked = sorted(
            variants,
            key=lambda variant: (
                repr(variant.key) not in selected,
                -dual_od.get(od, 0.0),
                variant.duration / max(1, variant.capacity),
                variant.duration,
                variant.key,
            ),
        )
        chosen = ranked[:per_od]
        imported += sum(repr(variant.key) in selected for variant in chosen)
        result[od] = tuple(chosen)
    return result, {
        "selected_route_keys_seen": len(selected),
        "top_dual_ods": len(dual_od),
        "per_od": per_od,
        "variants_in_primal_pool": sum(len(values) for values in result.values()),
        "selected_routes_imported": imported,
        "scope": "finite RMP ranking imported into primal LNS; not a global certificate",
    }


def build_flight_column_library(
    schedules: Sequence[tuple[str, Sequence[Q3Flight]]],
    people: dict[str, Q3Person],
    *,
    path: Path,
) -> dict[str, object]:
    columns: dict[tuple[object, ...], dict[str, object]] = {}
    for source, schedule in schedules:
        for flight in schedule:
            key = (
                repr(flight.variant.key),
                flight.start // 1440,
                flight.start,
                flight.aircraft_id,
            )
            record = columns.setdefault(
                key,
                {
                    "column_id": hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:20],
                    "route_key": repr(flight.variant.key),
                    "day": flight.start // 1440,
                    "start": flight.start,
                    "end": flight.end,
                    "aircraft_id": flight.aircraft_id,
                    "base": flight.variant.base_airport,
                    "aircraft_type": flight.variant.aircraft_type,
                    "duration": flight.duration,
                    "fuel_kg": flight.variant.fuel_kg,
                    "capacity": flight.variant.capacity,
                    "person_ids": sorted(flight.person_ids),
                    "mandatory_count": sum(people[pid].mandatory for pid in flight.person_ids),
                    "optional_count": sum(not people[pid].mandatory for pid in flight.person_ids),
                    "sources": [],
                    "elite_frequency": 0,
                    "reduced_cost": None,
                    "dual_score": None,
                },
            )
            record["sources"].append(source)
            record["elite_frequency"] = int(record["elite_frequency"]) + 1
    records = sorted(columns.values(), key=lambda row: (row["day"], row["start"], row["column_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, records)
    return {
        "column_count": len(records),
        "schedule_sources": len(schedules),
        "source_counts": dict(Counter(source for source, _schedule in schedules)),
        "path": str(path),
    }


def recombine_elites(
    pool: ElitePool,
    people: dict[str, Q3Person],
    data: ProblemData,
    *,
    pairs: int = 20,
    assignment_time_limit: float = 15.0,
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Restricted day-column set-partitioning heuristic plus path relinking."""

    best, relink = path_relink_and_recombine(
        pool,
        people,
        data,
        maximum_pairs=pairs,
        assignment_time_limit_seconds=assignment_time_limit,
    )
    rows: list[dict[str, object]] = []
    records = sorted(pool.records, key=lambda record: record.objective)
    candidate_pairs = [
        (left, right)
        for index, left in enumerate(records)
        for right in records[index + 1 :]
    ]
    candidate_pairs.sort(
        key=lambda pair: (
            abs(pair[0].objective[0] - pair[1].objective[0]),
            -solution_distance(pair[0].flights, pair[1].flights),
        )
    )
    best_key = stage1_key(best, people)
    for pair_index, (left, right) in enumerate(candidate_pairs[:pairs]):
        for mask in (0b0101010, 0b0011100, 0b1110000, 0b0001111):
            hybrid = [
                deepcopy(flight)
                for flight in left.flights
                if not (mask & (1 << (flight.start // 1440)))
            ] + [
                deepcopy(flight)
                for flight in right.flights
                if mask & (1 << (flight.start // 1440))
            ]
            try:
                repaired, unserved, exact = optimize_fixed_flight_assignments(
                    hybrid,
                    people,
                    data.config,
                    time_limit_seconds=assignment_time_limit,
                )
            except RuntimeError as exc:
                rows.append({"pair": pair_index, "mask": mask, "feasible": False, "error": str(exc)})
                continue
            repaired = [flight for flight in repaired if flight.person_ids]
            feasible = not unserved and int(schedule_metrics(repaired, people)["served_mandatory"]) == sum(
                person.mandatory for person in people.values()
            )
            improved = feasible and stage1_key(repaired, people) < best_key
            if improved:
                best = repaired
                best_key = stage1_key(best, people)
                pool.add(best, source=f"day-column-master:{pair_index}:{mask}", seed=0)
            rows.append(
                {
                    "pair": pair_index,
                    "mask": mask,
                    "feasible": feasible,
                    "improved": improved,
                    "fixed_assignment_optimal": exact.get("fixed_flight_optimal"),
                }
            )
    return best, {
        "path_relink": relink,
        "day_column_recombination": rows,
        "restricted_master_scope": "elite day-block columns with exact assignment repair",
    }


def guided_exact_lns(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    windows: int,
    seed: int,
    assignment_time_limit: float,
) -> tuple[list[Q3Flight], dict[str, object]]:
    incumbent = deepcopy(list(baseline))
    rows: list[dict[str, object]] = []
    operators = (
        "bottleneck_day",
        "conflict_graph",
        "aircraft_chain",
        "high_cost",
        "low_utilization",
    )
    for index in range(windows):
        operator = operators[index % len(operators)]
        group_min = (6, 8, 10)[index % 3]
        group_max = min(12, group_min + 2)
        before = stage1_key(incumbent, people)
        started = time.perf_counter()
        candidate, trace = generalized_multiflight_ruin_recreate(
            incumbent,
            people,
            variants_by_od,
            data,
            stage=1,
            group_min=group_min,
            group_max=group_max,
            maximum_trials=2,
            maximum_neighbors=30,
            route_limit=420,
            assignment_time_limit_seconds=assignment_time_limit,
            operator=operator,
            seed=seed + index,
            combination_budget=64,
            max_replacements=8,
        )
        accepted = stage1_key(candidate, people) < before
        if accepted:
            incumbent = candidate
        rows.append(
            {
                "window": index,
                "source": operator,
                "released_flights": [group_min, group_max],
                "accepted": accepted,
                "before_key": list(before),
                "after_key": list(stage1_key(incumbent, people)),
                "runtime_seconds": round(time.perf_counter() - started, 6),
                "trace": trace,
            }
        )
    return incumbent, {"windows": windows, "rows": rows}


def local_branching_search(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    radii: Sequence[int] = (5, 10, 20, 40, 80),
    seed: int = 0,
    assignment_time_limit: float = 10.0,
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Route-Hamming local-branching surrogate around the incumbent.

    The repository has no complete binary week master. Consequently this is a
    restricted route-change neighbourhood, not a global local-branching proof.
    """

    incumbent = deepcopy(list(baseline))
    rows = []
    for index, radius in enumerate(radii):
        before = stage1_key(incumbent, people)
        released = min(12, max(2, int(radius)))
        candidate, trace = generalized_multiflight_ruin_recreate(
            incumbent,
            people,
            variants_by_od,
            data,
            stage=1,
            group_min=max(2, min(6, released)),
            group_max=released,
            maximum_trials=2,
            maximum_neighbors=max(16, released * 2),
            route_limit=320,
            assignment_time_limit_seconds=assignment_time_limit,
            operator="random_related",
            seed=seed + index,
            combination_budget=min(64, max(12, radius * 2)),
            max_replacements=min(8, released),
        )
        accepted = stage1_key(candidate, people) < before
        if accepted:
            incumbent = candidate
        rows.append(
            {
                "requested_hamming_radius": radius,
                "released_route_limit": released,
                "accepted": accepted,
                "before_key": list(before),
                "after_key": list(stage1_key(incumbent, people)),
                "trace": trace,
            }
        )
    return incumbent, {
        "rows": rows,
        "scope": "restricted route-Hamming surrogate; complete binary master unavailable",
    }


def aircraft_day_chain_search(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    windows: int = 20,
    seed: int = 0,
    assignment_time_limit: float = 10.0,
) -> tuple[list[Q3Flight], dict[str, object]]:
    incumbent = deepcopy(list(baseline))
    rows = []
    for index in range(windows):
        before = stage1_key(incumbent, people)
        candidate, trace = generalized_multiflight_ruin_recreate(
            incumbent,
            people,
            variants_by_od,
            data,
            stage=1,
            group_min=3,
            group_max=10,
            maximum_trials=2,
            maximum_neighbors=24,
            route_limit=360,
            assignment_time_limit_seconds=assignment_time_limit,
            operator="aircraft_chain",
            seed=seed + index,
            combination_budget=40,
            max_replacements=8,
        )
        accepted = stage1_key(candidate, people) < before
        if accepted:
            incumbent = candidate
        rows.append(
            {
                "window": index,
                "accepted": accepted,
                "before_key": list(before),
                "after_key": list(stage1_key(incumbent, people)),
                "trace": trace,
            }
        )
    return incumbent, {"windows": windows, "rows": rows}


def checkpoint_payload(
    *,
    phase: str,
    completed: int,
    best: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    elite: ElitePool | None,
    route_library_version: str,
    column_library_version: str,
) -> dict[str, object]:
    return {
        "phase": phase,
        "completed": completed,
        "best_signature": solution_signature(best),
        "best_metrics": schedule_metrics(best, people),
        "elite": elite.summary() if elite is not None else None,
        "route_library_version": route_library_version,
        "column_library_version": column_library_version,
        "resume_semantics": "completed units are idempotently skipped; each unit has a deterministic seed",
    }


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def aggregate_convergence(
    traces: Iterable[dict[str, object]],
    *,
    baseline_ub: int,
    stage2_optional: int,
    global_lb: int,
    restricted_lp: float,
    route_count: int,
    column_count: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    offset = 0.0
    best = baseline_ub
    elite_size = 0
    for trace in traces:
        for point in trace.get("trace", {}).get("convergence", []):
            best = min(best, int(point["best_aircraft_time"]))
            elite_size = max(elite_size, int(point["elite_size"]))
            wall = offset + float(point["elapsed_seconds"])
            rows.append(
                {
                    "wall_time": round(wall, 6),
                    "stage1_ub": best,
                    "stage2_optional": stage2_optional,
                    "stage2_time": best,
                    "global_valid_lb": global_lb,
                    "restricted_lp": restricted_lp,
                    "gap_percent": 100.0 * (best - global_lb) / best,
                    "elite_pool_size": elite_size,
                    "route_count": route_count,
                    "column_count": column_count,
                }
            )
        offset += float(trace.get("runtime_seconds", 0.0))
    if not rows:
        rows.append(
            {
                "wall_time": 0.0,
                "stage1_ub": baseline_ub,
                "stage2_optional": stage2_optional,
                "stage2_time": baseline_ub,
                "global_valid_lb": global_lb,
                "restricted_lp": restricted_lp,
                "gap_percent": 100.0 * (baseline_ub - global_lb) / baseline_ub,
                "elite_pool_size": elite_size,
                "route_count": route_count,
                "column_count": column_count,
            }
        )
    return rows
