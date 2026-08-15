from __future__ import annotations

import hashlib
import math
import random
import time
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from ..io_utils import write_json
from .data import ProblemData
from .q3 import (
    Q3Flight,
    Q3Person,
    Q3Variant,
    _assignment_for_person,
    _compatible_aircraft,
    _person_day_intervals,
    optimize_fixed_flight_assignments,
    project_mandatory_only,
    retype_and_rehome_flights,
    schedule_metrics,
    shorten_fixed_flight_routes,
    stage1_key,
    stage2_key,
)
from .q3_closure_p2 import (
    cross_day_flexible_descent,
    generalized_multiflight_ruin_recreate,
    optional_feasibility_dossiers,
    targeted_optional_recovery,
)


@dataclass
class NeighborhoodCache:
    """Shared person-route-day and route-day-aircraft feasibility cache."""

    person_route_day: dict[
        tuple[str, tuple[object, ...], int], tuple[int, int, int, int] | None
    ] = field(default_factory=dict)
    route_day_aircraft: dict[
        tuple[tuple[object, ...], int, str], tuple[bool, int, int, int]
    ] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def feasible_person_day(
        self,
        person: Q3Person,
        variant: Q3Variant,
        day: int,
        data: ProblemData,
    ) -> tuple[int, int, int, int] | None:
        key = (person.person_id, variant.key, day)
        if key in self.person_route_day:
            self.hits += 1
            return self.person_route_day[key]
        self.misses += 1
        assignment = _assignment_for_person(person, variant, data.config)
        result = None
        if assignment is not None:
            for candidate_day, lower, upper in _person_day_intervals(
                person, variant, data.config, assignment
            ):
                if candidate_day == day:
                    result = (assignment[0], assignment[1], lower, upper)
                    break
        self.person_route_day[key] = result
        return result

    def feasible_route_aircraft(
        self,
        variant: Q3Variant,
        day: int,
        aircraft_id: str,
        data: ProblemData,
    ) -> tuple[bool, int, int, int]:
        key = (variant.key, day, aircraft_id)
        if key in self.route_day_aircraft:
            self.hits += 1
            return self.route_day_aircraft[key]
        self.misses += 1
        compatible = aircraft_id in _compatible_aircraft(data.config, variant)
        result = (
            compatible,
            variant.duration,
            day * 1440 + 360,
            day * 1440 + min(1080, 1200 - variant.duration),
        )
        self.route_day_aircraft[key] = result
        return result

    def stats(self) -> dict[str, object]:
        total = self.hits + self.misses
        return {
            "person_route_day_entries": len(self.person_route_day),
            "route_day_aircraft_entries": len(self.route_day_aircraft),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hits / total if total else 0.0,
        }


def _dominance_signature(variant: Q3Variant) -> tuple[object, ...]:
    """Only group routes with identical service and time-flexibility semantics."""

    return (
        variant.base_airport,
        variant.aircraft_type,
        tuple(
            (stop.facility_id, bool(stop.refuel))
            for stop in variant.source.route.stops
        ),
        tuple(variant.source.arrivals),
        tuple(variant.source.departures),
        variant.capacity,
    )


def preprocess_neighborhoods(
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
) -> tuple[
    dict[tuple[str, str], tuple[Q3Variant, ...]], NeighborhoodCache, dict[str, object]
]:
    """Prewarm safe feasibility caches and remove strictly dominated duplicates."""

    started = time.perf_counter()
    unique = {
        variant.key: variant
        for variants in variants_by_od.values()
        for variant in variants
    }
    groups: dict[tuple[object, ...], list[Q3Variant]] = defaultdict(list)
    for variant in unique.values():
        groups[_dominance_signature(variant)].append(variant)
    dominated: dict[tuple[object, ...], tuple[object, ...]] = {}
    for values in groups.values():
        ordered = sorted(values, key=lambda value: (value.duration, value.fuel_kg, value.key))
        champion = ordered[0]
        for candidate in ordered[1:]:
            if (
                champion.duration <= candidate.duration
                and champion.fuel_kg <= candidate.fuel_kg + 1e-9
                and (
                    champion.duration < candidate.duration
                    or champion.fuel_kg < candidate.fuel_kg - 1e-9
                )
            ):
                dominated[candidate.key] = champion.key

    filtered: dict[tuple[str, str], tuple[Q3Variant, ...]] = {}
    for od, values in variants_by_od.items():
        retained = [variant for variant in values if variant.key not in dominated]
        filtered[od] = tuple(
            sorted(
                retained,
                key=lambda variant: (
                    variant.duration / max(1, variant.capacity),
                    variant.duration,
                    variant.fuel_kg,
                    variant.key,
                ),
            )
        )

    cache = NeighborhoodCache()
    feasible_entries = 0
    for person in people.values():
        for variant in filtered.get(person.od, ()):
            assignment = _assignment_for_person(person, variant, data.config)
            if assignment is None:
                continue
            for day, lower, upper in _person_day_intervals(
                person, variant, data.config, assignment
            ):
                cache.person_route_day[(person.person_id, variant.key, day)] = (
                    assignment[0], assignment[1], lower, upper
                )
                feasible_entries += 1
    for variant in {v.key: v for values in filtered.values() for v in values}.values():
        for day in range(7):
            for aircraft_id in _compatible_aircraft(data.config, variant):
                cache.feasible_route_aircraft(variant, day, aircraft_id, data)
    # Exercise a deterministic sample of prewarmed entries so cache hit rate
    # measures the hot path without allocating millions of infeasible sentinels.
    for person_id, route_key, day in list(cache.person_route_day)[:1000]:
        person = people[person_id]
        variant = unique[route_key]
        cache.feasible_person_day(person, variant, day, data)

    report = {
        "candidate_count_before": len(unique),
        "candidate_count_after": len(
            {variant.key for values in filtered.values() for variant in values}
        ),
        "dominated_removed": len(dominated),
        "dominance_rule": "identical stop/refuel/order, relative timing and capacity; no cross-order dominance",
        "feasible_person_route_day_entries": feasible_entries,
        "cache": cache.stats(),
        "runtime_seconds": round(time.perf_counter() - started, 6),
    }
    return filtered, cache, report


def flight_structure_signature(flights: Sequence[Q3Flight]) -> tuple[object, ...]:
    return tuple(
        sorted(
            (
                flight.start // 1440,
                flight.aircraft_id,
                flight.start,
                flight.end,
                tuple(
                    (stop.facility_id, int(stop.refuel))
                    for stop in flight.variant.source.route.stops
                ),
            )
            for flight in flights
        )
    )


def solution_signature(flights: Sequence[Q3Flight]) -> str:
    passenger_days = sorted(
        (person_id, flight.start // 1440)
        for flight in flights
        for person_id in flight.person_ids
    )
    raw = repr((flight_structure_signature(flights), passenger_days)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def solution_distance(left: Sequence[Q3Flight], right: Sequence[Q3Flight]) -> float:
    left_routes = set(flight_structure_signature(left))
    right_routes = set(flight_structure_signature(right))
    route_distance = 1.0 - len(left_routes & right_routes) / max(1, len(left_routes | right_routes))
    left_days = {
        person_id: flight.start // 1440 for flight in left for person_id in flight.person_ids
    }
    right_days = {
        person_id: flight.start // 1440 for flight in right for person_id in flight.person_ids
    }
    common = set(left_days) & set(right_days)
    day_distance = (
        sum(left_days[pid] != right_days[pid] for pid in common) / max(1, len(common))
    )
    return 0.7 * route_distance + 0.3 * day_distance


@dataclass
class EliteRecord:
    flights: list[Q3Flight]
    source: str
    seed: int
    objective: tuple[float, ...]
    signature: str
    metrics: dict[str, object]
    validator_status: str = "pending_export_gate"


class ElitePool:
    def __init__(self, people: dict[str, Q3Person], *, stage: int, maximum_size: int = 20):
        self.people = people
        self.stage = stage
        self.maximum_size = maximum_size
        self.records: list[EliteRecord] = []

    def _key(self, flights: Sequence[Q3Flight]) -> tuple[float, ...]:
        return stage2_key(flights, self.people) if self.stage == 2 else stage1_key(flights, self.people)

    def add(self, flights: Sequence[Q3Flight], *, source: str, seed: int) -> bool:
        signature = solution_signature(flights)
        objective = self._key(flights)
        for index, record in enumerate(self.records):
            if record.signature != signature:
                continue
            if objective < record.objective:
                self.records[index] = EliteRecord(
                    deepcopy(list(flights)), source, seed, objective, signature,
                    schedule_metrics(flights, self.people),
                )
                self.records.sort(key=lambda value: value.objective)
                return True
            return False
        self.records.append(
            EliteRecord(
                deepcopy(list(flights)), source, seed, objective, signature,
                schedule_metrics(flights, self.people),
            )
        )
        self.records.sort(key=lambda value: value.objective)
        if len(self.records) > self.maximum_size:
            # Prefer removing a poor near-duplicate; otherwise remove the worst.
            removal = len(self.records) - 1
            worst_similarity = -1.0
            for index in range(1, len(self.records)):
                nearest = max(
                    1.0 - solution_distance(self.records[index].flights, other.flights)
                    for q, other in enumerate(self.records)
                    if q != index
                )
                if nearest > worst_similarity:
                    worst_similarity = nearest
                    removal = index
            self.records.pop(removal)
        return True

    @property
    def best(self) -> EliteRecord:
        return min(self.records, key=lambda value: value.objective)

    def summary(self) -> dict[str, object]:
        pairwise = [
            solution_distance(left.flights, right.flights)
            for i, left in enumerate(self.records)
            for right in self.records[i + 1 :]
        ]
        return {
            "size": len(self.records),
            "maximum_size": self.maximum_size,
            "best_objective": list(self.best.objective) if self.records else None,
            "mean_pairwise_distance": sum(pairwise) / len(pairwise) if pairwise else 0.0,
            "records": [
                {
                    "source": record.source,
                    "seed": record.seed,
                    "objective": list(record.objective),
                    "signature": record.signature,
                    "metrics": record.metrics,
                    "validator_status": record.validator_status,
                }
                for record in self.records
            ],
        }


@dataclass
class OperatorStats:
    attempts: int = 0
    feasible: int = 0
    accepted: int = 0
    global_best: int = 0
    total_saving: int = 0
    total_runtime: float = 0.0
    timeouts: int = 0
    weight: float = 1.0


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    names = list(weights)
    values = [max(0.01, weights[name]) for name in names]
    return rng.choices(names, weights=values, k=1)[0]


def long_horizon_alns(
    seeds: Sequence[tuple[str, Sequence[Q3Flight]]],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    iterations: int = 500,
    wall_time_seconds: float = 3600.0,
    seed: int = 20260816,
    restart_threshold: int = 80,
    assignment_time_limit_seconds: float = 15.0,
    elite_size: int = 20,
) -> tuple[list[Q3Flight], ElitePool, dict[str, object]]:
    """Adaptive long-horizon Stage 1 search with elite restarts and heavy ruin."""

    started = time.perf_counter()
    rng = random.Random(seed)
    pool = ElitePool(people, stage=1, maximum_size=elite_size)
    for source, flights in seeds:
        pool.add(flights, source=source, seed=seed)
    current = deepcopy(pool.best.flights)
    best = deepcopy(pool.best.flights)
    best_key = stage1_key(best, people)
    operators = (
        "related",
        "high_cost",
        "low_utilization",
        "aircraft_chain",
        "bottleneck_day",
        "conflict_graph",
        "random_related",
        "cross_day",
        "exact_lns",
        "route_polish",
    )
    stats = {name: OperatorStats() for name in operators}
    stats["exact_lns"].weight = 0.15
    stats["cross_day"].weight = 0.35
    convergence: list[dict[str, object]] = []
    failure_histogram: Counter[str] = Counter()
    stagnation = 0
    restarts = 0
    improvements = 0

    for iteration in range(1, iterations + 1):
        elapsed = time.perf_counter() - started
        if elapsed >= wall_time_seconds:
            break
        operator = _weighted_choice(rng, {name: stats[name].weight for name in operators})
        op = stats[operator]
        op.attempts += 1
        op_started = time.perf_counter()
        before_key = stage1_key(current, people)
        candidate = deepcopy(current)
        trace: dict[str, object] = {}
        try:
            if operator == "cross_day":
                candidate, trace = cross_day_flexible_descent(
                    current,
                    people,
                    variants_by_od,
                    data,
                    stage=1,
                    stage1_cap=None,
                    minimum_optional_served=0,
                    maximum_trials=4,
                )
            elif operator == "route_polish":
                candidate, shorten = shorten_fixed_flight_routes(
                    current, people, variants_by_od, data.config
                )
                candidate, retype = retype_and_rehome_flights(
                    candidate, people, variants_by_od, data.config, maximum_passes=1
                )
                trace = {"shorten": shorten, "retype": retype}
            else:
                heavy = operator == "exact_lns" or stagnation >= restart_threshold // 2
                group_min, group_max = ((4, 8) if heavy else (2, 5))
                rr_operator = "aircraft_chain" if operator == "exact_lns" else operator
                candidate, trace = generalized_multiflight_ruin_recreate(
                    current,
                    people,
                    variants_by_od,
                    data,
                    stage=1,
                    group_min=group_min,
                    group_max=group_max,
                    maximum_trials=1,
                    maximum_neighbors=14 if heavy else 10,
                    route_limit=180 if heavy else 100,
                    assignment_time_limit_seconds=min(
                        assignment_time_limit_seconds, 5.0 if heavy else 3.0
                    ),
                    operator=rr_operator,
                    seed=seed + iteration,
                    combination_budget=8 if heavy else 3,
                    max_replacements=5 if heavy else 4,
                )
            candidate_metrics = schedule_metrics(candidate, people)
            if int(candidate_metrics["served_mandatory"]) == len(people):
                op.feasible += 1
            for reason, count in dict(trace.get("rejection_reasons", {})).items():
                failure_histogram[str(reason)] += int(count)
        except (RuntimeError, ValueError) as exc:
            failure_histogram[type(exc).__name__] += 1
            trace = {"error": str(exc)}

        candidate_key = stage1_key(candidate, people)
        accepted = candidate_key < before_key
        global_improvement = candidate_key < best_key
        saving = 0
        if accepted:
            saving = int(before_key[0] - candidate_key[0])
            current = candidate
            op.accepted += 1
            op.total_saving += saving
            pool.add(current, source=f"alns:{operator}:{iteration}", seed=seed)
        if global_improvement:
            best = deepcopy(candidate)
            best_key = candidate_key
            op.global_best += 1
            improvements += 1
            stagnation = 0
        else:
            stagnation += 1

        runtime = time.perf_counter() - op_started
        op.total_runtime += runtime
        reward = 8.0 if global_improvement else 4.0 if accepted else 0.5 if candidate_key == before_key else 0.0
        op.weight = max(0.05, 0.85 * op.weight + 0.15 * reward)

        restarted = False
        if stagnation >= restart_threshold and pool.records:
            # Reheating/restart deliberately permits current to be worse than
            # global best while the immutable global best remains monotone.
            ranked = pool.records[: max(1, min(len(pool.records), 8))]
            current = deepcopy(rng.choice(ranked).flights)
            stagnation = 0
            restarts += 1
            restarted = True

        convergence.append(
            {
                "iteration": iteration,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "operator": operator,
                "accepted": accepted,
                "global_best": global_improvement,
                "saving_minutes": saving,
                "current_aircraft_time": int(stage1_key(current, people)[0]),
                "best_aircraft_time": int(best_key[0]),
                "stagnation": stagnation,
                "restart": restarted,
                "elite_size": len(pool.records),
            }
        )

    return best, pool, {
        "requested_iterations": iterations,
        "completed_iterations": len(convergence),
        "wall_time_seconds": round(time.perf_counter() - started, 6),
        "seed": seed,
        "restart_threshold": restart_threshold,
        "restarts": restarts,
        "global_improvements": improvements,
        "operator_stats": {name: asdict(value) for name, value in stats.items()},
        "failure_reason_histogram": dict(sorted(failure_histogram.items())),
        "convergence": convergence,
    }


def path_relink_and_recombine(
    pool: ElitePool,
    people: dict[str, Q3Person],
    data: ProblemData,
    *,
    maximum_pairs: int = 12,
    assignment_time_limit_seconds: float = 20.0,
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Bidirectional day-block path relinking over diverse elite schedules."""

    if not pool.records:
        raise ValueError("elite pool is empty")
    best = deepcopy(pool.best.flights)
    best_key = stage1_key(best, people)
    rows: list[dict[str, object]] = []
    pairs = [
        (left, right)
        for index, left in enumerate(pool.records)
        for right in pool.records[index + 1 :]
    ]
    pairs.sort(key=lambda pair: -solution_distance(pair[0].flights, pair[1].flights))
    for pair_index, (left, right) in enumerate(pairs[:maximum_pairs]):
        for direction, (source, guide) in enumerate(((left, right), (right, left))):
            current_days: set[int] = set()
            for day in range(7):
                current_days.add(day)
                hybrid = [
                    deepcopy(flight)
                    for flight in source.flights
                    if flight.start // 1440 not in current_days
                ] + [
                    deepcopy(flight)
                    for flight in guide.flights
                    if flight.start // 1440 in current_days
                ]
                try:
                    repaired, unserved, milp = optimize_fixed_flight_assignments(
                        hybrid,
                        people,
                        data.config,
                        time_limit_seconds=assignment_time_limit_seconds,
                    )
                except RuntimeError as exc:
                    rows.append(
                        {
                            "pair": pair_index,
                            "direction": direction,
                            "step": day + 1,
                            "feasible": False,
                            "error": str(exc),
                        }
                    )
                    continue
                repaired = [flight for flight in repaired if flight.person_ids]
                metrics = schedule_metrics(repaired, people)
                feasible = int(metrics["served_mandatory"]) == len(people) and not unserved
                key = stage1_key(repaired, people)
                improved = feasible and key < best_key
                if improved:
                    best, best_key = deepcopy(repaired), key
                    pool.add(best, source=f"path-relink:{pair_index}:{direction}:{day}", seed=0)
                rows.append(
                    {
                        "pair": pair_index,
                        "direction": direction,
                        "step": day + 1,
                        "feasible": feasible,
                        "improved": improved,
                        "metrics": metrics,
                        "fixed_flight_optimal": milp.get("fixed_flight_optimal"),
                    }
                )
    return best, {"attempts": len(rows), "rows": rows}


def exact_fix_and_optimize(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    windows: int = 24,
    seed: int = 20260816,
    assignment_time_limit_seconds: float = 30.0,
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Rolling exact-LNS intensification with 4--8-flight released blocks."""

    incumbent = deepcopy(list(baseline))
    rows: list[dict[str, object]] = []
    operators = ("aircraft_chain", "high_cost", "bottleneck_day", "conflict_graph")
    for window in range(windows):
        before = schedule_metrics(incumbent, people)
        operator = operators[window % len(operators)]
        candidate, trace = generalized_multiflight_ruin_recreate(
            incumbent,
            people,
            variants_by_od,
            data,
            stage=1,
            group_min=4,
            group_max=8,
            maximum_trials=2,
            maximum_neighbors=18,
            route_limit=240,
            assignment_time_limit_seconds=assignment_time_limit_seconds,
            operator=operator,
            seed=seed + window,
            combination_budget=16,
            max_replacements=6,
        )
        accepted = stage1_key(candidate, people) < stage1_key(incumbent, people)
        if accepted:
            incumbent = candidate
        after = schedule_metrics(incumbent, people)
        rows.append(
            {
                "window": window,
                "operator": operator,
                "accepted": accepted,
                "before": before,
                "after": after,
                "trace": trace,
            }
        )
    return incumbent, {"windows": windows, "rows": rows}


def optimize_stage2_under_cap(
    stage1: Sequence[Q3Flight],
    incumbent_stage2: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    trials: int = 60,
    assignment_time_limit_seconds: float = 30.0,
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Targeted optional rescue, ejection/exchange and exact fixed-flight polish."""

    cap = int(schedule_metrics(stage1, people)["total_aircraft_time_minutes"])
    candidates: list[tuple[str, list[Q3Flight], dict[str, object]]] = []
    fixed, unserved, fixed_stats = optimize_fixed_flight_assignments(
        stage1, people, data.config, time_limit_seconds=assignment_time_limit_seconds
    )
    candidates.append(("stage1-fixed-master", fixed, fixed_stats))
    if int(schedule_metrics(incumbent_stage2, people)["total_aircraft_time_minutes"]) <= cap:
        candidates.append(("incumbent", deepcopy(list(incumbent_stage2)), {}))
    start = min(candidates, key=lambda item: stage2_key(item[1], people))[1]
    rescued, rescue = targeted_optional_recovery(
        start,
        people,
        variants_by_od,
        data,
        stage1_cap=cap,
        maximum_trials=trials,
        assignment_time_limit_seconds=min(assignment_time_limit_seconds, 5.0),
        combination_budget=4,
    )
    targets = [row["person_id"] for row in optional_feasibility_dossiers(rescued, people, variants_by_od, data)]
    current = rescued
    operator_rows = []
    for index, operator in enumerate(("optional_target", "conflict_graph", "aircraft_chain", "random_related")):
        candidate, trace = generalized_multiflight_ruin_recreate(
            current,
            people,
            variants_by_od,
            data,
            stage=2,
            stage1_cap=cap,
            minimum_optional_served=int(schedule_metrics(current, people)["served_optional"]),
            group_min=3,
            group_max=8,
            maximum_trials=max(1, min(3, trials // 20)),
            maximum_neighbors=16,
            route_limit=220,
            assignment_time_limit_seconds=min(assignment_time_limit_seconds, 5.0),
            target_optional_ids=targets,
            operator=operator,
            seed=20260816 + index,
            combination_budget=4,
            max_replacements=6,
        )
        if stage2_key(candidate, people) < stage2_key(current, people):
            current = candidate
        operator_rows.append({"operator": operator, "trace": trace, "metrics": schedule_metrics(current, people)})
    return current, {
        "cap": cap,
        "initial_fixed_master": fixed_stats,
        "initial_unserved": unserved,
        "targeted_rescue": rescue,
        "operators": operator_rows,
        "final_dossiers": optional_feasibility_dossiers(current, people, variants_by_od, data),
    }


def build_route_library(
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    final_flights: Sequence[Q3Flight],
    *,
    source: str,
    path: Path,
) -> dict[str, object]:
    """Persist deduplicated route metadata without mutating the Q2 cache."""

    support: dict[tuple[object, ...], set[tuple[str, str]]] = defaultdict(set)
    unique: dict[tuple[object, ...], Q3Variant] = {}
    for od, variants in variants_by_od.items():
        for variant in variants:
            unique[variant.key] = variant
            support[variant.key].add(od)
    usage = Counter(flight.variant.key for flight in final_flights)
    records = []
    for key, variant in sorted(unique.items(), key=lambda item: repr(item[0])):
        records.append(
            {
                "route_key": repr(key),
                "base": variant.base_airport,
                "aircraft_type": variant.aircraft_type,
                "stops": [stop.facility_id for stop in variant.source.route.stops],
                "refuel_pattern": [bool(stop.refuel) for stop in variant.source.route.stops],
                "duration": variant.duration,
                "fuel_kg": variant.fuel_kg,
                "capacity": variant.capacity,
                "supported_ods": [list(od) for od in sorted(support[key])],
                "usage_count": usage[key],
                "elite_frequency": int(usage[key] > 0),
                "historical_saving": None,
                "source": source if usage[key] else "q2-read-only-cache",
                "dual_or_reduced_cost": None,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, records)
    return {
        "route_count": len(records),
        "used_by_final": sum(record["usage_count"] > 0 for record in records),
        "source_counts": dict(Counter(str(record["source"]) for record in records)),
        "path": str(path),
    }
