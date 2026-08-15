from __future__ import annotations

import hashlib
import math
import random
import time
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from itertools import combinations, permutations
from pathlib import Path
from typing import Iterable, Sequence

from .data import ProblemData
from .q2 import assignment_interval, build_q2_variant
from .q3 import (
    Q3Flight,
    Q3Person,
    Q3PersonFlexibility,
    Q3Variant,
    _assignment_for_person,
    _best_group_option,
    _compatible_aircraft,
    _flight_accepts_person,
    _person_day_intervals,
    _schedule_candidate_on_aircraft,
    build_flexibility_profiles,
    build_mandatory_schedule,
    optimize_fixed_flight_assignments,
    schedule_comparison_key,
    schedule_metrics,
    schedule_seat_utilization,
    stage1_key,
    stage2_key,
)
from .q3_timing import schedule_route_timing


def _seat_utilization_proxy(flights: Sequence[Q3Flight]) -> float:
    """Compatibility alias for the canonical actual-timing implementation."""

    return schedule_seat_utilization(flights)


def is_hard_person(
    person: Q3Person,
    profile: Q3PersonFlexibility,
    *,
    day_threshold: int = 1,
    window_minutes: int = 720,
    scarce_route_threshold: int = 2,
) -> bool:
    return (
        person.task_type in {"emergency", "production"}
        or profile.feasible_day_count <= day_threshold
        or profile.window_width <= window_minutes
        or profile.compatible_variant_count <= scarce_route_threshold
    )


def build_day_pressure(
    people: Iterable[Q3Person],
    profiles: dict[str, Q3PersonFlexibility],
    variants: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
) -> dict[int, float]:
    pressure: dict[int, float] = defaultdict(float)
    for person in people:
        feasible_days = {
            day
            for variant in variants[person.od]
            for day, _lo, _hi in _person_day_intervals(
                person, variant, data.config
            )
        }
        if not feasible_days:
            continue
        profile = profiles[person.person_id]
        weight = (
            1.0
            + 4.0 / max(1, profile.feasible_day_count)
            + 2.0 / max(1, profile.compatible_variant_count)
            + 720.0 / max(60, profile.window_width)
        )
        for day in feasible_days:
            pressure[day] += weight / len(feasible_days)
    return {day: round(pressure.get(day, 0.0), 6) for day in range(7)}


def _calendar_from_flights(
    flights: Sequence[Q3Flight], data: ProblemData
) -> dict[str, list[tuple[int, int]]]:
    calendars = {aircraft_id: [] for aircraft_id in data.config.fleet_ids}
    for flight in flights:
        calendars[flight.aircraft_id].append((flight.start, flight.end))
    for values in calendars.values():
        values.sort()
    return calendars


def _fragmentation_score(
    intervals: Sequence[tuple[int, int]], start: int, end: int, day: int
) -> tuple[int, int]:
    day_start, day_end = day * 1440 + 360, day * 1440 + 1200
    values = sorted(
        [(max(day_start, left), min(day_end, right)) for left, right in intervals]
        + [(start, end)]
    )
    gaps: list[int] = []
    cursor = day_start
    for left, right in values:
        if right <= day_start or left >= day_end:
            continue
        gaps.append(max(0, left - cursor))
        cursor = max(cursor, right)
    gaps.append(max(0, day_end - cursor))
    positive = [gap for gap in gaps if gap > 0]
    return (len(positive), -max(positive, default=0))


def _place_group_time_aware(
    variant: Q3Variant,
    selected: Sequence[Q3Person],
    assignments: dict[str, tuple[int, int]],
    people: dict[str, Q3Person],
    day: int,
    calendars: dict[str, list[tuple[int, int]]],
    data: ProblemData,
    slot_policy: str,
) -> tuple[str, object] | None:
    choices: list[tuple[tuple[object, ...], str, object]] = []
    selected_assignments = {
        person.person_id: assignments[person.person_id] for person in selected
    }
    for aircraft_id in _compatible_aircraft(data.config, variant):
        timing = _schedule_candidate_on_aircraft(
            variant,
            selected_assignments,
            people,
            day,
            aircraft_id,
            calendars,
            data.config,
        )
        if timing is None:
            continue
        if slot_policy == "least_fragmentation":
            fragment = _fragmentation_score(
                calendars[aircraft_id],
                timing.departures[0],
                timing.arrivals[-1],
                day,
            )
        elif slot_policy == "best_fit":
            day_start = day * 1440 + 360
            day_end = day * 1440 + 1200
            prior_end = max(
                (end for start, end in calendars[aircraft_id] if end <= timing.departures[0]),
                default=day_start - data.config.turnaround_minutes,
            )
            next_start = min(
                (start for start, end in calendars[aircraft_id] if start >= timing.arrivals[-1]),
                default=day_end + data.config.turnaround_minutes,
            )
            fragment = (
                max(0, timing.departures[0] - prior_end - data.config.turnaround_minutes)
                + max(0, next_start - timing.arrivals[-1] - data.config.turnaround_minutes),
                timing.departures[0],
            )
        else:
            fragment = (0, timing.departures[0])
        score = (
            fragment,
            timing.duration,
            sum(timing.waiting_minutes),
            aircraft_id,
        )
        choices.append((score, aircraft_id, timing))
    if not choices:
        return None
    _score, aircraft_id, timing = min(choices)
    return aircraft_id, timing


def build_mandatory_schedule_flexible_regret(
    people: dict[str, Q3Person],
    variants: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    seed: int = 0,
    hard_day_threshold: int = 1,
    hard_window_minutes: int = 720,
    scarce_route_threshold: int = 2,
    regret_k: int = 2,
    fleet_slot_policy: str = "least_fragmentation",
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Two-phase hard-skeleton constructor with dynamic regret repair.

    Hard tasks alone create the skeleton.  Flexible tasks first receive
    zero-aircraft-time insertions.  The remaining tasks are reconsidered after
    every new flight; their best current option and regret proxy therefore
    depend on the live fleet calendars rather than a fixed global sort.
    """

    started = time.perf_counter()
    mandatory = {pid: p for pid, p in people.items() if p.mandatory}
    profiles = build_flexibility_profiles(mandatory.values(), variants, data.config)
    hard_ids = {
        pid
        for pid, person in mandatory.items()
        if is_hard_person(
            person,
            profiles[pid],
            day_threshold=hard_day_threshold,
            window_minutes=hard_window_minutes,
            scarce_route_threshold=scarce_route_threshold,
        )
    }
    hard_people = {pid: mandatory[pid] for pid in hard_ids}
    skeleton, skeleton_stats = build_mandatory_schedule(
        hard_people,
        variants,
        data,
        mode="criticality",
        seed=seed,
        flexibility_profiles={pid: profiles[pid] for pid in hard_ids},
        hard_day_threshold=hard_day_threshold,
        hard_window_minutes=hard_window_minutes,
    )
    if not skeleton_stats.feasible:
        return skeleton, {
            "feasible": False,
            "phase": "hard_skeleton",
            "hard_count": len(hard_ids),
            "skeleton": skeleton_stats.to_dict(),
            "runtime_seconds": round(time.perf_counter() - started, 6),
        }

    remaining = set(mandatory) - hard_ids
    day_pressure = build_day_pressure(mandatory.values(), profiles, variants, data)
    zero_cost_inserted = 0
    for person_id in sorted(
        list(remaining),
        key=lambda pid: (
            profiles[pid].feasible_day_count,
            mandatory[pid].latest,
            pid,
        ),
    ):
        person = mandatory[person_id]
        choices = [
            (flight, assignment)
            for flight in skeleton
            if (assignment := _flight_accepts_person(
                flight, person, mandatory, data.config
            ))
            is not None
        ]
        if not choices:
            continue
        flight, assignment = min(
            choices,
            key=lambda item: (
                day_pressure[item[0].start // 1440],
                item[0].duration,
                item[0].aircraft_id,
            ),
        )
        flight.person_ids.append(person_id)
        flight.assignment_intervals[person_id] = assignment
        remaining.remove(person_id)
        zero_cost_inserted += 1

    calendars = _calendar_from_flights(skeleton, data)
    dynamic_steps: list[dict[str, object]] = []
    rng = random.Random(seed)
    while remaining:
        # Recompute a live option for a bounded critical shortlist.  This keeps
        # the constructor practical on 3840 tasks while preserving dynamic
        # regret semantics after each accepted flight.
        shortlist = sorted(
            remaining,
            key=lambda pid: (
                profiles[pid].feasible_day_count,
                -profiles[pid].regret_proxy,
                mandatory[pid].latest,
                rng.random(),
                pid,
            ),
        )[:1]
        live: list[tuple[tuple[object, ...], str, object]] = []
        remaining_people = [mandatory[pid] for pid in sorted(remaining)]
        for person_id in shortlist:
            person = mandatory[person_id]
            option = _best_group_option(
                person,
                remaining_people,
                variants[person.od],
                calendars,
                data.config,
            )
            if option is None:
                continue
            variant, aircraft_id, start, selected, assignments = option
            current_cost = variant.duration / max(1, len(selected))
            profile = profiles[person_id]
            single_option = profile.compatible_variant_count <= 1
            dynamic_regret = (
                10**9 if single_option else profile.regret_proxy
            ) + current_cost
            score = (
                -dynamic_regret,
                profile.feasible_day_count,
                day_pressure[start // 1440],
                current_cost,
                person_id,
            )
            live.append((score, person_id, option))
        if not live:
            return skeleton, {
                "feasible": False,
                "phase": "dynamic_regret",
                "failed_people": sorted(remaining)[:20],
                "hard_count": len(hard_ids),
                "zero_cost_inserted": zero_cost_inserted,
                "runtime_seconds": round(time.perf_counter() - started, 6),
            }
        _score, seed_id, option = min(live)
        variant, _old_aircraft, start, selected, assignments = option
        day = start // 1440
        placed = _place_group_time_aware(
            variant,
            selected,
            assignments,
            mandatory,
            day,
            calendars,
            data,
            fleet_slot_policy,
        )
        if placed is None:
            selected = [mandatory[seed_id]]
            assignments = {
                seed_id: _assignment_for_person(
                    mandatory[seed_id], variant, data.config
                )
            }
            if assignments[seed_id] is None:
                return skeleton, {
                    "feasible": False,
                    "phase": "time_aware_place",
                    "failed_people": [seed_id],
                    "hard_count": len(hard_ids),
                    "zero_cost_inserted": zero_cost_inserted,
                    "dynamic_flights": len(dynamic_steps),
                    "runtime_seconds": round(time.perf_counter() - started, 6),
                }
            placed = _place_group_time_aware(
                variant,
                selected,
                assignments,  # type: ignore[arg-type]
                mandatory,
                day,
                calendars,
                data,
                fleet_slot_policy,
            )
        if placed is None:
            singleton = mandatory[seed_id]
            alternatives: list[tuple[tuple[object, ...], Q3Variant, int, dict[str, tuple[int, int]], tuple[str, object]]] = []
            for alternative in variants[singleton.od]:
                assignment = _assignment_for_person(singleton, alternative, data.config)
                if assignment is None:
                    continue
                for alternative_day, _lo, _hi in _person_day_intervals(
                    singleton, alternative, data.config, assignment
                ):
                    alternative_assignments = {seed_id: assignment}
                    alternative_place = _place_group_time_aware(
                        alternative,
                        [singleton],
                        alternative_assignments,
                        mandatory,
                        alternative_day,
                        calendars,
                        data,
                        fleet_slot_policy,
                    )
                    if alternative_place is None:
                        continue
                    _aircraft, alternative_timing = alternative_place
                    alternatives.append(
                        (
                            (
                                alternative_timing.duration,
                                day_pressure[alternative_day],
                                alternative.fuel_kg,
                                alternative_day,
                            ),
                            alternative,
                            alternative_day,
                            alternative_assignments,
                            alternative_place,
                        )
                    )
            if not alternatives:
                return skeleton, {
                    "feasible": False,
                    "phase": "fleet_slot",
                    "failed_people": [seed_id],
                    "hard_count": len(hard_ids),
                    "zero_cost_inserted": zero_cost_inserted,
                    "dynamic_flights": len(dynamic_steps),
                    "runtime_seconds": round(time.perf_counter() - started, 6),
                }
            _alt_score, variant, day, assignments, placed = min(
                alternatives, key=lambda item: item[0]
            )
            selected = [singleton]
        aircraft_id, timing = placed
        selected = [person for person in selected if person.person_id in remaining]
        selected_assignments = {
            person.person_id: assignments[person.person_id] for person in selected
        }
        flight = Q3Flight(
            variant=variant,
            aircraft_id=aircraft_id,
            start=timing.departures[0],
            person_ids=[person.person_id for person in selected],
            assignment_intervals=selected_assignments,
            timing=timing,
        )
        skeleton.append(flight)
        calendars[aircraft_id].append((flight.start, flight.end))
        calendars[aircraft_id].sort()
        for person in selected:
            remaining.discard(person.person_id)
        dynamic_steps.append(
            {
                "seed": seed_id,
                "selected": len(selected),
                "day": day,
                "aircraft_id": aircraft_id,
                "duration": flight.duration,
                "waiting": sum(timing.waiting_minutes),
            }
        )

    return skeleton, {
        "feasible": True,
        "hard_count": len(hard_ids),
        "flexible_count": len(mandatory) - len(hard_ids),
        "zero_cost_inserted": zero_cost_inserted,
        "dynamic_flights": len(dynamic_steps),
        "day_pressure": day_pressure,
        "fleet_slot_policy": fleet_slot_policy,
        "skeleton": skeleton_stats.to_dict(),
        "steps": dynamic_steps,
        "runtime_seconds": round(time.perf_counter() - started, 6),
    }


@dataclass(frozen=True)
class _ConcreteOption:
    variant: Q3Variant
    aircraft_id: str
    timing: object
    assignments: dict[str, tuple[int, int]]


def _relatedness(left: Q3Flight, right: Q3Flight) -> float:
    left_nodes = {
        stop.facility_id for stop in left.variant.source.route.stops[1:-1]
    }
    right_nodes = {
        stop.facility_id for stop in right.variant.source.route.stops[1:-1]
    }
    overlap = len(left_nodes & right_nodes) / max(1, len(left_nodes | right_nodes))
    same_base = left.variant.base_airport == right.variant.base_airport
    same_aircraft = left.aircraft_id == right.aircraft_id
    proximity = max(0.0, 1.0 - abs(left.start - right.start) / 720.0)
    return 3.0 * overlap + float(same_base) + 0.5 * float(same_aircraft) + proximity


def _no_conflict(
    options: Sequence[_ConcreteOption], turnaround: int
) -> bool:
    by_aircraft: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for option in options:
        timing = option.timing
        by_aircraft[option.aircraft_id].append(
            (timing.departures[0], timing.arrivals[-1])
        )
    for intervals in by_aircraft.values():
        intervals.sort()
        if any(
            left_end + turnaround > right_start
            for (_left_start, left_end), (right_start, _right_end) in zip(
                intervals, intervals[1:]
            )
        ):
            return False
    return True


def _concrete_options(
    ruined_ids: Sequence[str],
    day: int,
    unaffected: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    route_limit: int,
) -> list[_ConcreteOption]:
    unique: dict[tuple[object, ...], Q3Variant] = {}
    cover_count: Counter[tuple[object, ...]] = Counter()
    for person_id in ruined_ids:
        for variant in variants_by_od[people[person_id].od]:
            unique[variant.key] = variant
            cover_count[variant.key] += 1
    candidates = sorted(
        unique.values(),
        key=lambda variant: (
            -cover_count[variant.key],
            variant.duration,
            -variant.capacity,
            variant.key,
        ),
    )[:route_limit]
    calendars = {aircraft_id: [] for aircraft_id in data.config.fleet_ids}
    for flight in unaffected:
        calendars[flight.aircraft_id].append((flight.start, flight.end))
    for values in calendars.values():
        values.sort()
    options: list[_ConcreteOption] = []
    signatures: set[tuple[object, ...]] = set()
    for variant in candidates:
        compatible = []
        for person_id in ruined_ids:
            assignment = _assignment_for_person(
                people[person_id], variant, data.config
            )
            if assignment is None:
                continue
            if not any(
                q == day
                for q, _lo, _hi in _person_day_intervals(
                    people[person_id], variant, data.config, assignment
                )
            ):
                continue
            compatible.append((person_id, assignment))
        if not compatible:
            continue
        orders = [
            sorted(compatible, key=lambda item: (people[item[0]].latest, item[0])),
            sorted(compatible, key=lambda item: (-people[item[0]].earliest, item[0])),
        ]
        for order in orders:
            chosen: dict[str, tuple[int, int]] = {}
            loads = [0] * (len(variant.source.route.stops) - 1)
            for person_id, assignment in order:
                pickup, delivery = assignment
                if any(loads[leg] >= variant.capacity for leg in range(pickup, delivery)):
                    continue
                trial = {**chosen, person_id: assignment}
                timing = schedule_route_timing(
                    variant, trial, people, day, data.config
                )
                if timing is None:
                    continue
                chosen = trial
                for leg in range(pickup, delivery):
                    loads[leg] += 1
            if not chosen:
                continue
            for aircraft_id in _compatible_aircraft(data.config, variant):
                timing = _schedule_candidate_on_aircraft(
                    variant,
                    chosen,
                    people,
                    day,
                    aircraft_id,
                    calendars,
                    data.config,
                )
                if timing is None:
                    continue
                signature = (
                    variant.key,
                    aircraft_id,
                    timing.departures[0],
                    tuple(sorted(chosen)),
                )
                if signature in signatures:
                    continue
                signatures.add(signature)
                options.append(
                    _ConcreteOption(
                        variant=variant,
                        aircraft_id=aircraft_id,
                        timing=timing,
                        assignments=chosen,
                    )
                )
    options.sort(
        key=lambda option: (
            -len(option.assignments),
            option.timing.duration,
            sum(option.timing.waiting_minutes),
            option.variant.fuel_kg,
            option.aircraft_id,
        )
    )
    return options


def generalized_multiflight_ruin_recreate(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    stage: int = 1,
    stage1_cap: int | None = None,
    minimum_optional_served: int = 0,
    group_min: int = 2,
    group_max: int = 4,
    maximum_trials: int = 50,
    maximum_neighbors: int = 8,
    route_limit: int = 100,
    assignment_time_limit_seconds: float = 20.0,
    target_optional_ids: Sequence[str] = (),
    operator: str = "related",
    seed: int = 0,
    combination_budget: int = 12,
    max_replacements: int | None = None,
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Static-pool same-day k-to-m structural neighbourhood.

    Replacement count is variable and may equal the removed count, allowing a
    genuine k-to-k shorter move.  The bounded combination budget keeps this
    evaluator suitable both for screening and exact-LNS-style intensification.
    """

    started = time.perf_counter()
    incumbent = deepcopy(list(baseline))
    required_mandatory = sum(person.mandatory for person in people.values())
    rejections: Counter[str] = Counter()
    accepted: list[dict[str, object]] = []
    trials = 0
    rng = random.Random(seed)

    while trials < maximum_trials:
        before = schedule_metrics(incumbent, people)
        indices = list(range(len(incumbent)))
        if operator == "high_cost":
            indices.sort(key=lambda i: (-incumbent[i].duration, i))
        elif operator == "low_utilization":
            indices.sort(key=lambda i: (len(incumbent[i].person_ids), -incumbent[i].duration, i))
        elif operator == "aircraft_chain":
            indices.sort(key=lambda i: (incumbent[i].aircraft_id, incumbent[i].start, i))
        elif operator == "random_related":
            rng.shuffle(indices)
        elif operator == "bottleneck_day":
            day_cost = Counter(
                flight.start // 1440 for flight in incumbent for _ in range(flight.duration)
            )
            indices.sort(
                key=lambda i: (-day_cost[incumbent[i].start // 1440], -incumbent[i].duration, i)
            )
        elif operator == "optional_target":
            targets = set(target_optional_ids)
            indices.sort(
                key=lambda i: (
                    -sum(pid in targets for pid in incumbent[i].person_ids),
                    -incumbent[i].duration,
                    i,
                )
            )
        elif operator == "conflict_graph":
            indices.sort(
                key=lambda i: (
                    -max(
                        (
                            sum(
                                pickup <= leg < delivery
                                for pickup, delivery in incumbent[i].assignment_intervals.values()
                            )
                            for leg in range(len(incumbent[i].variant.source.route.stops) - 1)
                        ),
                        default=0,
                    ),
                    -incumbent[i].duration,
                    i,
                )
            )
        else:
            indices.sort(key=lambda i: (incumbent[i].start // 1440, -incumbent[i].duration, i))
        improved = False
        for anchor in indices:
            if trials >= maximum_trials:
                break
            day = incumbent[anchor].start // 1440
            neighbors = [
                index
                for index in range(len(incumbent))
                if index != anchor and incumbent[index].start // 1440 == day
            ]
            neighbors.sort(
                key=lambda index: (-_relatedness(incumbent[anchor], incumbent[index]), index)
            )
            if operator == "random_related":
                rng.shuffle(neighbors)
            elif operator == "aircraft_chain":
                neighbors.sort(
                    key=lambda index: (
                        incumbent[index].aircraft_id != incumbent[anchor].aircraft_id,
                        abs(incumbent[index].start - incumbent[anchor].start),
                        index,
                    )
                )
            neighbors = neighbors[:maximum_neighbors]
            for group_size in range(group_min, min(group_max, 1 + len(neighbors)) + 1):
                if trials >= maximum_trials:
                    break
                selected_indices = tuple([anchor] + neighbors[: group_size - 1])
                trials += 1
                selected_set = set(selected_indices)
                selected = [incumbent[index] for index in selected_indices]
                ruined_ids = sorted(
                    {person_id for flight in selected for person_id in flight.person_ids}
                    | {
                        pid
                        for pid in target_optional_ids
                        if pid in people and not people[pid].mandatory
                    }
                )
                unaffected = [
                    deepcopy(flight)
                    for index, flight in enumerate(incumbent)
                    if index not in selected_set
                ]
                options = _concrete_options(
                    ruined_ids,
                    day,
                    unaffected,
                    people,
                    variants_by_od,
                    data,
                    route_limit=route_limit,
                )[:24]
                if not options:
                    rejections["no_concrete_option"] += 1
                    continue
                old_time = sum(flight.duration for flight in selected)
                best_candidate: list[Q3Flight] | None = None
                best_key: tuple[float, ...] | None = None
                best_move: tuple[int, int] | None = None
                replacement_limit = min(
                    group_size,
                    max_replacements if max_replacements is not None else 4,
                )
                examined = 0
                for replacement_count in range(1, replacement_limit + 1):
                    for option_set in combinations(options, replacement_count):
                        examined += 1
                        if examined > combination_budget:
                            break
                        if sum(option.timing.duration for option in option_set) >= old_time:
                            continue
                        if not _no_conflict(option_set, data.config.turnaround_minutes):
                            continue
                        replacements = [
                            Q3Flight(
                                variant=option.variant,
                                aircraft_id=option.aircraft_id,
                                start=option.timing.departures[0],
                                person_ids=list(option.assignments),
                                assignment_intervals=dict(option.assignments),
                                timing=option.timing,
                            )
                            for option in option_set
                        ]
                        try:
                            repaired, _unserved, _milp = optimize_fixed_flight_assignments(
                                unaffected + replacements,
                                people,
                                data.config,
                                time_limit_seconds=assignment_time_limit_seconds,
                            )
                        except RuntimeError:
                            continue
                        repaired = [flight for flight in repaired if flight.person_ids]
                        metrics = schedule_metrics(repaired, people)
                        if int(metrics["served_mandatory"]) != required_mandatory:
                            continue
                        if int(metrics["served_optional"]) < minimum_optional_served:
                            continue
                        if stage1_cap is not None and int(
                            metrics["total_aircraft_time_minutes"]
                        ) > stage1_cap:
                            continue
                        key = stage2_key(repaired, people) if stage == 2 else stage1_key(repaired, people)
                        incumbent_key = stage2_key(incumbent, people) if stage == 2 else stage1_key(incumbent, people)
                        primary_ok = (
                            key < incumbent_key
                            if stage == 2
                            else int(metrics["total_aircraft_time_minutes"])
                            < int(before["total_aircraft_time_minutes"])
                        )
                        if not primary_ok:
                            continue
                        if best_key is None or key < best_key:
                            best_candidate = repaired
                            best_key = key
                            best_move = (group_size, len(replacements))
                    if examined > combination_budget:
                        break
                if best_candidate is None or best_move is None:
                    rejections["no_accepted_repair"] += 1
                    continue
                after = schedule_metrics(best_candidate, people)
                old_count, new_count = best_move
                incumbent = best_candidate
                accepted.append(
                    {
                        "move": f"{old_count}->{new_count}",
                        "old_aircraft_time_minutes": before["total_aircraft_time_minutes"],
                        "new_aircraft_time_minutes": after["total_aircraft_time_minutes"],
                        "saved_minutes": int(before["total_aircraft_time_minutes"])
                        - int(after["total_aircraft_time_minutes"]),
                        "old_flights": before["total_flights"],
                        "new_flights": after["total_flights"],
                        "served_optional": after["served_optional"],
                        "day": day,
                        "operator": operator,
                    }
                )
                improved = True
                break
            if improved:
                break
        if not improved:
            break

    histogram = Counter(str(move["move"]) for move in accepted)
    return incumbent, {
        "trials": trials,
        "accepted_count": len(accepted),
        "accepted_histogram": dict(sorted(histogram.items())),
        "saved_aircraft_time_minutes": sum(int(move["saved_minutes"]) for move in accepted),
        "runtime_seconds": round(time.perf_counter() - started, 6),
        "rejection_reasons": dict(sorted(rejections.items())),
        "moves": accepted,
        "group_min": group_min,
        "group_max": group_max,
        "combination_budget": combination_budget,
        "max_replacements": max_replacements,
        "static_pool_only": True,
    }


def optional_feasibility_dossiers(
    flights: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
) -> list[dict[str, object]]:
    assigned = {pid for flight in flights for pid in flight.person_ids}
    dossiers: list[dict[str, object]] = []
    for person in sorted(
        (p for p in people.values() if not p.mandatory and p.person_id not in assigned),
        key=lambda p: p.person_id,
    ):
        variants = variants_by_od[person.od]
        days = sorted(
            {
                day
                for variant in variants
                for day, _lo, _hi in _person_day_intervals(person, variant, data.config)
            }
        )
        covering = []
        time_compatible = []
        seat_compatible = []
        for index, flight in enumerate(flights):
            assignment = _assignment_for_person(person, flight.variant, data.config)
            if assignment is None:
                continue
            covering.append(index)
            pickup, delivery = assignment
            if (
                flight.departures[pickup] >= person.earliest
                and flight.arrivals[delivery] <= person.latest
            ):
                time_compatible.append(index)
                if _flight_accepts_person(flight, person, people, data.config) is not None:
                    seat_compatible.append(index)
        if seat_compatible:
            blocker = "A_fixed_assignment_repair"
        elif time_compatible:
            blocker = "A_fixed_flights_no_seat"
        elif covering:
            blocker = "B_timing_incompatible"
        elif variants:
            blocker = "C_no_matching_incumbent_flight"
        else:
            blocker = "D_route_universe"
        dossiers.append(
            {
                "person_id": person.person_id,
                "od": list(person.od),
                "earliest": person.earliest,
                "latest": person.latest,
                "feasible_days": days,
                "compatible_cached_variants": len(variants),
                "minimum_isolated_route_duration": min(v.duration for v in variants),
                "existing_covering_flights": covering,
                "time_compatible_flights": time_compatible,
                "seat_compatible_flights": seat_compatible,
                "primary_blocker": blocker,
            }
        )
    return dossiers


def targeted_optional_recovery(
    stage2: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    stage1_cap: int,
    maximum_trials: int = 60,
    assignment_time_limit_seconds: float = 30.0,
    combination_budget: int = 12,
) -> tuple[list[Q3Flight], dict[str, object]]:
    started = time.perf_counter()
    incumbent, unserved, fixed_stats = optimize_fixed_flight_assignments(
        stage2,
        people,
        data.config,
        time_limit_seconds=assignment_time_limit_seconds,
    )
    before = schedule_metrics(incumbent, people)
    targets = list(unserved)
    trace: list[dict[str, object]] = [
        {"level": "L0", "metrics": before, "targets": targets, "assignment": fixed_stats}
    ]
    if targets:
        for level, group_max in (("L1", 2), ("L2", 3), ("L3", 5)):
            candidate, rr = generalized_multiflight_ruin_recreate(
                incumbent,
                people,
                variants_by_od,
                data,
                stage=2,
                stage1_cap=stage1_cap,
                minimum_optional_served=int(schedule_metrics(incumbent, people)["served_optional"]),
                group_min=2,
                group_max=group_max,
                maximum_trials=max(1, maximum_trials // 3),
                assignment_time_limit_seconds=assignment_time_limit_seconds,
                target_optional_ids=targets,
                operator="related",
                seed=group_max,
                combination_budget=combination_budget,
            )
            if stage2_key(candidate, people) < stage2_key(incumbent, people):
                incumbent = candidate
            trace.append({"level": level, "metrics": schedule_metrics(incumbent, people), "rr": rr})
            assigned = {pid for flight in incumbent for pid in flight.person_ids}
            targets = sorted(pid for pid, p in people.items() if not p.mandatory and pid not in assigned)
            if not targets:
                break
    after = schedule_metrics(incumbent, people)
    return incumbent, {
        "before": before,
        "after": after,
        "targets_before": unserved,
        "targets_after": targets,
        "recovered": sorted(set(unserved) - set(targets)),
        "levels": trace,
        "runtime_seconds": round(time.perf_counter() - started, 6),
    }


def adaptive_structural_lns(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    stage: int,
    stage1_cap: int | None,
    minimum_optional_served: int,
    trials_per_operator: int = 12,
    seed: int = 0,
) -> tuple[list[Q3Flight], dict[str, object]]:
    operators = (
        "related",
        "high_cost",
        "low_utilization",
        "aircraft_chain",
        "random_related",
    )
    incumbent = deepcopy(list(baseline))
    weights = {operator: 1.0 for operator in operators}
    rows: list[dict[str, object]] = []
    for round_index, operator in enumerate(operators):
        before = schedule_metrics(incumbent, people)
        candidate, trace = generalized_multiflight_ruin_recreate(
            incumbent,
            people,
            variants_by_od,
            data,
            stage=stage,
            stage1_cap=stage1_cap,
            minimum_optional_served=minimum_optional_served,
            group_min=3,
            group_max=6,
            maximum_trials=trials_per_operator,
            maximum_neighbors=10,
            route_limit=120,
            assignment_time_limit_seconds=20.0,
            operator=operator,
            seed=seed + round_index,
        )
        accepted = (
            stage2_key(candidate, people) < stage2_key(incumbent, people)
            if stage == 2
            else stage1_key(candidate, people) < stage1_key(incumbent, people)
        )
        if accepted:
            incumbent = candidate
            weights[operator] = 0.8 * weights[operator] + 0.2 * 4.0
        else:
            weights[operator] *= 0.8
        after = schedule_metrics(incumbent, people)
        rows.append(
            {
                "operator": operator,
                "attempts": trace["trials"],
                "accepted": trace["accepted_count"],
                "saved_minutes": int(before["total_aircraft_time_minutes"])
                - int(after["total_aircraft_time_minutes"]),
                "weight_after": round(weights[operator], 6),
                "histogram": trace["accepted_histogram"],
            }
        )
    return incumbent, {"operator_stats": rows, "weights": weights, "seed": seed}


def augment_dynamic_route_pool(
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    target_ids: Sequence[str],
    people: dict[str, Q3Person],
    flights: Sequence[Q3Flight],
    data: ProblemData,
    *,
    maximum_sequences: int = 30,
) -> tuple[dict[tuple[str, str], tuple[Q3Variant, ...]], dict[str, object]]:
    involved = {
        node
        for person_id in target_ids
        for node in people[person_id].od
        if node in data.config.facilities
    }
    for flight in flights:
        nodes = {stop.facility_id for stop in flight.variant.source.route.stops[1:-1]}
        if nodes & involved:
            involved.update(nodes)
    nodes = sorted(involved)
    sequences: list[tuple[str, ...]] = []
    for length in range(1, min(4, len(nodes)) + 1):
        for sequence in permutations(nodes, length):
            sequences.append(sequence)
            if len(sequences) >= maximum_sequences:
                break
        if len(sequences) >= maximum_sequences:
            break
    generated: dict[tuple[object, ...], Q3Variant] = {}
    for sequence in sequences:
        for base in data.config.airports:
            for aircraft_type in sorted(data.config.aircraft_types):
                source = build_q2_variant(data, base, aircraft_type, sequence)
                if source is not None:
                    generated[source.key] = Q3Variant(source)
    result: dict[tuple[str, str], tuple[Q3Variant, ...]] = {}
    additions = 0
    for od, old_values in variants_by_od.items():
        merged = {variant.key: variant for variant in old_values}
        for variant in generated.values():
            if assignment_interval(
                variant.source, od[0], od[1], data.config.airports
            ) is not None and variant.key not in merged:
                merged[variant.key] = variant
                additions += 1
        result[od] = tuple(
            sorted(
                merged.values(),
                key=lambda variant: (
                    variant.duration / variant.capacity,
                    variant.duration,
                    -variant.capacity,
                    variant.key,
                ),
            )
        )
    return result, {
        "target_ids": list(target_ids),
        "facility_universe": nodes,
        "sequences_considered": len(sequences),
        "unique_dynamic_variants": len(generated),
        "od_variant_additions": additions,
        "q2_cache_written": False,
    }


def cross_day_flexible_descent(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    stage: int,
    stage1_cap: int | None,
    minimum_optional_served: int,
    maximum_trials: int = 30,
) -> tuple[list[Q3Flight], dict[str, object]]:
    profiles = build_flexibility_profiles(
        (person for person in people.values() if person.mandatory),
        variants_by_od,
        data.config,
    )
    incumbent = deepcopy(list(baseline))
    moves: list[dict[str, object]] = []
    trials = 0
    candidates = sorted(
        range(len(incumbent)),
        key=lambda index: (len(incumbent[index].person_ids), -incumbent[index].duration),
    )
    for index in candidates:
        if trials >= maximum_trials or index >= len(incumbent):
            break
        flight = incumbent[index]
        mandatory_ids = [pid for pid in flight.person_ids if people[pid].mandatory]
        if not mandatory_ids:
            continue
        if not all(
            people[pid].task_type == "shift"
            and profiles[pid].feasible_day_count > 1
            and profiles[pid].window_width > 720
            for pid in mandatory_ids
        ):
            continue
        trials += 1
        trial = [deepcopy(value) for q, value in enumerate(incumbent) if q != index]
        try:
            repaired, _unserved, _stats = optimize_fixed_flight_assignments(
                trial, people, data.config, time_limit_seconds=30.0
            )
        except RuntimeError:
            continue
        metrics = schedule_metrics(repaired, people)
        if int(metrics["served_mandatory"]) != sum(p.mandatory for p in people.values()):
            continue
        if int(metrics["served_optional"]) < minimum_optional_served:
            continue
        if stage1_cap is not None and int(metrics["total_aircraft_time_minutes"]) > stage1_cap:
            continue
        better = (
            stage2_key(repaired, people) < stage2_key(incumbent, people)
            if stage == 2
            else stage1_key(repaired, people) < stage1_key(incumbent, people)
        )
        if not better:
            continue
        old_day = flight.start // 1440
        assigned_after = {
            pid: candidate.start // 1440
            for candidate in repaired
            for pid in candidate.person_ids
        }
        moved = [pid for pid in mandatory_ids if assigned_after.get(pid) != old_day]
        incumbent = repaired
        moves.append(
            {
                "removed_aircraft": flight.aircraft_id,
                "removed_start": flight.start,
                "moved_passengers": moved,
                "saved_minutes": flight.duration,
            }
        )
        break
    return incumbent, {"trials": trials, "accepted": len(moves), "moves": moves}


def route_cache_provenance(path: Path, variant_count: int) -> dict[str, object]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_epoch": stat.st_mtime,
        "sha256": digest,
        "variant_count": variant_count,
    }


def public_stats(value: object) -> object:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value
