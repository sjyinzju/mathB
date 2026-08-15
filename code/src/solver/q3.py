from __future__ import annotations

import csv
import heapq
import math
import pickle
import random
import time
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from ..config import ProblemConfig
from ..data_pipeline import TIME_FORMAT
from ..io_utils import write_csv
from .data import ProblemData
from .q2 import Q2RouteVariant, assignment_interval
from .q3_timing import Q3FlightTiming, schedule_route_timing


@dataclass(frozen=True)
class Q3Person:
    person_id: str
    origin_id: str
    destination_id: str
    earliest: int
    latest: int
    task_type: str
    priority: int

    @property
    def od(self) -> tuple[str, str]:
        return self.origin_id, self.destination_id

    @property
    def mandatory(self) -> bool:
        return self.task_type != "temporary"


@dataclass(frozen=True)
class Q3Variant:
    source: Q2RouteVariant

    @property
    def base_airport(self) -> str:
        return self.source.base_airport

    @property
    def aircraft_type(self) -> str:
        return self.source.aircraft_type

    @property
    def capacity(self) -> int:
        return self.source.capacity

    @property
    def duration(self) -> int:
        return self.source.evaluation.total_aircraft_time_minutes

    @property
    def fuel_kg(self) -> float:
        return self.source.evaluation.total_fuel_consumption_kg

    @property
    def key(self) -> tuple[object, ...]:
        return self.source.key


@dataclass
class Q3Flight:
    variant: Q3Variant
    aircraft_id: str
    start: int
    person_ids: list[str] = field(default_factory=list)
    assignment_intervals: dict[str, tuple[int, int]] = field(default_factory=dict)
    flight_no: int = 0
    timing: Q3FlightTiming | None = None

    @property
    def duration(self) -> int:
        return self.timing.duration if self.timing is not None else self.variant.duration

    @property
    def arrivals(self) -> tuple[int, ...]:
        if self.timing is not None:
            return self.timing.arrivals
        return tuple(self.start + value for value in self.variant.source.arrivals)

    @property
    def departures(self) -> tuple[int, ...]:
        if self.timing is not None:
            return self.timing.departures
        return tuple(self.start + value for value in self.variant.source.departures)

    @property
    def end(self) -> int:
        return self.arrivals[-1]

@dataclass(frozen=True)
class Q3ScheduleStats:
    method: str
    seed: int
    feasible: bool
    mandatory_people: int
    flights: int
    aircraft_time_minutes: int
    lower_bound_minutes: int
    lower_bound_gap_percent: float
    runtime_seconds: float
    conflict_count: int
    candidate_variants: int
    failed_person_id: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Q3PersonFlexibility:
    feasible_day_count: int
    compatible_variant_count: int
    best_duration: int
    second_best_duration: int
    regret_proxy: int
    od_demand: int
    window_width: int


def build_flexibility_profiles(
    people: Iterable[Q3Person],
    variants: dict[tuple[str, str], tuple[Q3Variant, ...]],
    config: ProblemConfig,
) -> dict[str, Q3PersonFlexibility]:
    people_list = list(people)
    od_demand = Counter(person.od for person in people_list if person.mandatory)
    profiles: dict[str, Q3PersonFlexibility] = {}
    for person in people_list:
        compatible = variants[person.od]
        feasible_days = {
            day
            for variant in compatible
            for day, _lower, _upper in _person_day_intervals(person, variant, config)
        }
        durations = sorted({variant.duration for variant in compatible})
        best = durations[0]
        second = durations[1] if len(durations) > 1 else best + 10**6
        profiles[person.person_id] = Q3PersonFlexibility(
            feasible_day_count=len(feasible_days),
            compatible_variant_count=len(compatible),
            best_duration=best,
            second_best_duration=second,
            regret_proxy=second - best,
            od_demand=od_demand[person.od],
            window_width=person.latest - person.earliest,
        )
    return profiles


def load_q3_people(path: Path, config: ProblemConfig) -> dict[str, Q3Person]:
    people: dict[str, Q3Person] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            earliest_dt = datetime.strptime(row["earliest_pickup_time"], TIME_FORMAT)
            latest_dt = datetime.strptime(row["latest_arrival_time"], TIME_FORMAT)
            earliest = round((earliest_dt - config.planning_start).total_seconds() / 60)
            latest = round((latest_dt - config.planning_start).total_seconds() / 60)
            person = Q3Person(
                person_id=row["person_id"],
                origin_id=row["origin_id"],
                destination_id=row["destination_id"],
                earliest=earliest,
                latest=latest,
                task_type=row["task_type"],
                priority=config.task_priority[row["task_type"]],
            )
            people[person.person_id] = person
    return people


def load_q3_variants(
    cache_path: Path,
    people: Iterable[Q3Person],
    config: ProblemConfig,
) -> dict[tuple[str, str], tuple[Q3Variant, ...]]:
    with cache_path.open("rb") as stream:
        cached = pickle.load(stream)
    q2_variants: Sequence[Q2RouteVariant] = cached["variants"]
    by_od: dict[tuple[str, str], list[Q3Variant]] = defaultdict(list)
    ods = sorted({person.od for person in people})
    for od in ods:
        origin, destination = od
        for variant in q2_variants:
            interval = assignment_interval(variant, origin, destination, config.airports)
            if interval is None:
                continue
            by_od[od].append(Q3Variant(variant))
        if not by_od[od]:
            raise ValueError(f"Q3 candidate cache does not cover OD {od}")
        unique = {variant.key: variant for variant in by_od[od]}
        by_od[od] = sorted(
            unique.values(),
            key=lambda variant: (
                variant.duration / variant.capacity,
                variant.duration,
                -variant.capacity,
                variant.base_airport,
                variant.aircraft_type,
                variant.key,
            ),
        )
    return {od: tuple(values) for od, values in by_od.items()}


def _assignment_for_person(
    person: Q3Person,
    variant: Q3Variant,
    config: ProblemConfig,
) -> tuple[int, int] | None:
    interval = assignment_interval(
        variant.source,
        person.origin_id,
        person.destination_id,
        config.airports,
    )
    return (interval[0], interval[1]) if interval is not None else None


def _person_day_intervals(
    person: Q3Person,
    variant: Q3Variant,
    config: ProblemConfig,
    assignment: tuple[int, int] | None = None,
) -> tuple[tuple[int, int, int], ...]:
    assignment = assignment or _assignment_for_person(person, variant, config)
    if assignment is None:
        return ()
    pickup, delivery = assignment
    lower = person.earliest - variant.source.departures[pickup]
    upper = person.latest - variant.source.arrivals[delivery]
    values: list[tuple[int, int, int]] = []
    for day in range(7):
        offset = 1440 * day
        lo = max(lower, offset + 360)
        hi = min(upper, offset + 1080, offset + 1200 - variant.duration)
        if lo <= hi:
            values.append((day, lo, hi))
    return tuple(values)


def _compatible_aircraft(config: ProblemConfig, variant: Q3Variant) -> tuple[str, ...]:
    prefix = f"{variant.base_airport}-{variant.aircraft_type}-"
    return tuple(aircraft for aircraft in config.fleet_ids if aircraft.startswith(prefix))


def _first_calendar_slot(
    intervals: Sequence[tuple[int, int]],
    lower: int,
    upper: int,
    duration: int,
    turnaround: int,
) -> int | None:
    candidate = lower
    for start, end in sorted(intervals):
        if end + turnaround <= candidate:
            continue
        if candidate + duration + turnaround <= start:
            return candidate
        candidate = max(candidate, end + turnaround)
        if candidate > upper:
            return None
    return candidate if candidate <= upper else None


def _fleet_slot(
    calendars: dict[str, list[tuple[int, int]]],
    config: ProblemConfig,
    variant: Q3Variant,
    lower: int,
    upper: int,
) -> tuple[str, int] | None:
    choices: list[tuple[int, int, str]] = []
    for aircraft_id in _compatible_aircraft(config, variant):
        slot = _first_calendar_slot(
            calendars[aircraft_id],
            lower,
            upper,
            variant.duration,
            config.turnaround_minutes,
        )
        if slot is not None:
            used = sum(end - start for start, end in calendars[aircraft_id])
            choices.append((slot, used, aircraft_id))
    if not choices:
        return None
    slot, _, aircraft_id = min(choices)
    return aircraft_id, slot


def _seed_key(
    person: Q3Person,
    mode: str,
    rng: random.Random,
    profile: Q3PersonFlexibility | None = None,
    *,
    hard_day_threshold: int = 1,
    hard_window_minutes: int = 720,
) -> tuple[object, ...]:
    slack = person.latest - person.earliest
    noise = rng.random()
    if profile is not None:
        if mode == "day_scarcity":
            return (
                profile.feasible_day_count,
                person.priority,
                person.latest,
                slack,
                noise,
                person.person_id,
            )
        if mode == "route_scarcity":
            return (
                profile.compatible_variant_count,
                profile.feasible_day_count,
                person.latest,
                noise,
                person.person_id,
            )
        if mode in {"criticality", "randomized_criticality"}:
            return (
                person.priority,
                profile.feasible_day_count,
                profile.compatible_variant_count,
                slack,
                person.latest,
                noise if mode.startswith("randomized") else 0.0,
                person.person_id,
            )
        if mode == "od_density":
            return (
                person.priority,
                -profile.od_demand,
                profile.feasible_day_count,
                person.latest,
                noise,
                person.person_id,
            )
        if mode == "regret_proxy":
            return (
                -profile.regret_proxy,
                profile.feasible_day_count,
                person.priority,
                person.latest,
                noise,
                person.person_id,
            )
        if mode == "flexible_regret":
            hard = (
                person.task_type in {"emergency", "production"}
                or profile.feasible_day_count <= hard_day_threshold
                or profile.window_width <= hard_window_minutes
                or profile.compatible_variant_count <= 2
            )
            return (
                0 if hard else 1,
                -profile.regret_proxy,
                profile.feasible_day_count,
                person.priority,
                person.latest,
                -profile.od_demand,
                noise,
                person.person_id,
            )
    if mode == "deadline":
        return person.latest, person.priority, slack, noise, person.person_id
    if mode == "randomized_deadline":
        return person.latest, person.priority, slack, noise, person.person_id
    if mode == "slack":
        return slack, person.priority, person.latest, noise, person.person_id
    return person.priority, person.latest, slack, noise, person.person_id


def _best_group_option(
    seed: Q3Person,
    remaining_people: Sequence[Q3Person],
    variants: Sequence[Q3Variant],
    calendars: dict[str, list[tuple[int, int]]],
    config: ProblemConfig,
) -> tuple[Q3Variant, str, int, list[Q3Person], dict[str, tuple[int, int]]] | None:
    best: tuple[
        tuple[object, ...],
        Q3Variant,
        str,
        int,
        list[Q3Person],
        dict[str, tuple[int, int]],
    ] | None = None
    for variant in variants:
        assignments = {
            person.person_id: value
            for person in remaining_people
            if (value := _assignment_for_person(person, variant, config)) is not None
        }
        if seed.person_id not in assignments:
            continue
        intervals = {
            person.person_id: _person_day_intervals(
                person, variant, config, assignments[person.person_id]
            )
            for person in remaining_people
            if person.person_id in assignments
        }
        for day, seed_lo, seed_hi in intervals[seed.person_id]:
            same_day: dict[str, tuple[int, int]] = {}
            for person in remaining_people:
                if person.person_id not in intervals:
                    continue
                match = next(
                    ((lo, hi) for q, lo, hi in intervals[person.person_id] if q == day),
                    None,
                )
                if match is not None and match[0] <= seed_hi and match[1] >= seed_lo:
                    same_day[person.person_id] = match
            event_times = {seed_lo, seed_hi}
            for lo, hi in same_day.values():
                event_times.add(max(seed_lo, lo))
                event_times.add(min(seed_hi, hi))
            for probe in sorted(event_times):
                eligible = [
                    person
                    for person in remaining_people
                    if person.person_id in same_day
                    and same_day[person.person_id][0] <= probe <= same_day[person.person_id][1]
                ]
                eligible.sort(
                    key=lambda person: (
                        person.priority,
                        person.latest,
                        person.latest - person.earliest,
                        person.person_id,
                    )
                )
                # Interval packing on route legs permits seat reuse after a
                # passenger disembarks. The seed is inserted first; remaining
                # passengers are accepted only if every occupied leg stays legal.
                selected = [seed]
                leg_loads = [0] * (len(variant.source.route.stops) - 1)
                seed_pickup, seed_delivery = assignments[seed.person_id]
                for leg in range(seed_pickup, seed_delivery):
                    leg_loads[leg] += 1
                for person in eligible:
                    if person.person_id == seed.person_id:
                        continue
                    pickup, delivery = assignments[person.person_id]
                    if all(leg_loads[leg] < variant.capacity for leg in range(pickup, delivery)):
                        selected.append(person)
                        for leg in range(pickup, delivery):
                            leg_loads[leg] += 1
                lower = max(same_day[person.person_id][0] for person in selected)
                upper = min(same_day[person.person_id][1] for person in selected)
                if lower > upper:
                    continue
                fleet = _fleet_slot(calendars, config, variant, lower, upper)
                if fleet is None:
                    continue
                aircraft_id, start = fleet
                score = (
                    variant.duration / len(selected),
                    -len(selected),
                    variant.duration,
                    start,
                    -variant.capacity,
                    variant.base_airport,
                    variant.aircraft_type,
                )
                if best is None or score < best[0]:
                    best = (
                        score,
                        variant,
                        aircraft_id,
                        start,
                        selected,
                        {person.person_id: assignments[person.person_id] for person in selected},
                    )
    if best is None:
        return None
    _, variant, aircraft_id, start, selected, selected_assignments = best
    return variant, aircraft_id, start, selected, selected_assignments


def transport_time_lower_bound(
    people: Iterable[Q3Person],
    data: ProblemData,
) -> int:
    nodes = list(data.config.nodes)
    index = {node: pos for pos, node in enumerate(nodes)}
    distance = [[float(data.matrix[left][right]) for right in nodes] for left in nodes]
    for pivot in range(len(nodes)):
        for left in range(len(nodes)):
            through = distance[left][pivot]
            for right in range(len(nodes)):
                candidate = through + distance[pivot][right]
                if candidate < distance[left][right]:
                    distance[left][right] = candidate
    passenger_km = 0.0
    for person in people:
        origins = data.config.airports if person.origin_id == "LAND" else (person.origin_id,)
        destinations = (
            data.config.airports if person.destination_id == "LAND" else (person.destination_id,)
        )
        passenger_km += min(
            distance[index[origin]][index[destination]]
            for origin in origins
            for destination in destinations
        )
    maximum_seat_km_per_minute = max(
        aircraft.seats * aircraft.speed_kmh / 60.0
        for aircraft in data.config.aircraft_types.values()
    )
    return math.ceil(passenger_km / maximum_seat_km_per_minute)


def build_mandatory_schedule(
    people: dict[str, Q3Person],
    variants: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    mode: str = "priority",
    seed: int = 0,
    guide_optional_ids: Sequence[str] = (),
    flexibility_profiles: dict[str, Q3PersonFlexibility] | None = None,
    hard_day_threshold: int = 1,
    hard_window_minutes: int = 720,
) -> tuple[list[Q3Flight], Q3ScheduleStats]:
    started = time.perf_counter()
    mandatory = {pid: person for pid, person in people.items() if person.mandatory}
    remaining = set(mandatory)
    guide_remaining = {
        person_id
        for person_id in guide_optional_ids
        if person_id in people and not people[person_id].mandatory
    }
    by_od: dict[tuple[str, str], set[str]] = defaultdict(set)
    for person in mandatory.values():
        by_od[person.od].add(person.person_id)
    rng = random.Random(seed)
    profiles = flexibility_profiles or build_flexibility_profiles(
        mandatory.values(), variants, data.config
    )
    heap = [
        (
            _seed_key(
                person,
                mode,
                rng,
                profiles.get(person.person_id),
                hard_day_threshold=hard_day_threshold,
                hard_window_minutes=hard_window_minutes,
            ),
            person.person_id,
        )
        for person in mandatory.values()
    ]
    heapq.heapify(heap)
    calendars: dict[str, list[tuple[int, int]]] = {
        aircraft_id: [] for aircraft_id in data.config.fleet_ids
    }
    flights: list[Q3Flight] = []
    conflicts = 0
    while remaining:
        while heap and heap[0][1] not in remaining:
            heapq.heappop(heap)
        if not heap:
            break
        _, seed_id = heapq.heappop(heap)
        seed_person = mandatory[seed_id]
        # Optional guide people participate only in route packing.  They may
        # steer a bottleneck OD toward a larger aircraft/route without becoming
        # a first-stage coverage requirement.  They are stripped from the
        # returned baseline after construction.
        remaining_people = [mandatory[pid] for pid in sorted(remaining)] + [
            people[pid] for pid in sorted(guide_remaining)
        ]
        option = _best_group_option(
            seed_person,
            remaining_people,
            variants[seed_person.od],
            calendars,
            data.config,
        )
        if option is None:
            conflicts += 1
            # Conflict feedback: retry the task as a singleton on every route variant.
            option = _best_group_option(
                seed_person,
                [seed_person],
                variants[seed_person.od],
                calendars,
                data.config,
            )
        if option is None:
            lower_bound = transport_time_lower_bound(mandatory.values(), data)
            stats = Q3ScheduleStats(
                method=f"q3_interval_list_{mode}",
                seed=seed,
                feasible=False,
                mandatory_people=len(mandatory) - len(remaining),
                flights=len(flights),
                aircraft_time_minutes=sum(flight.variant.duration for flight in flights),
                lower_bound_minutes=lower_bound,
                lower_bound_gap_percent=float("inf"),
                runtime_seconds=round(time.perf_counter() - started, 6),
                conflict_count=conflicts + 1,
                candidate_variants=sum(len(values) for values in variants.values()),
                failed_person_id=seed_person.person_id,
            )
            return flights, stats
        variant, aircraft_id, start, selected, selected_assignments = option
        flight = Q3Flight(
            variant=variant,
            aircraft_id=aircraft_id,
            start=start,
            person_ids=[person.person_id for person in selected],
            assignment_intervals=selected_assignments,
        )
        flights.append(flight)
        calendars[aircraft_id].append((flight.start, flight.end))
        calendars[aircraft_id].sort()
        for person in selected:
            if person.mandatory:
                remaining.discard(person.person_id)
            else:
                guide_remaining.discard(person.person_id)
    for flight in flights:
        guided = [pid for pid in flight.person_ids if not people[pid].mandatory]
        for person_id in guided:
            flight.person_ids.remove(person_id)
            flight.assignment_intervals.pop(person_id, None)
    lower_bound = transport_time_lower_bound(mandatory.values(), data)
    total_time = sum(flight.variant.duration for flight in flights)
    stats = Q3ScheduleStats(
        method=(
            f"q3_interval_list_{mode}"
            + (f"_guided{len(guide_optional_ids)}" if guide_optional_ids else "")
        ),
        seed=seed,
        feasible=not remaining,
        mandatory_people=len(mandatory) - len(remaining),
        flights=len(flights),
        aircraft_time_minutes=total_time,
        lower_bound_minutes=lower_bound,
        lower_bound_gap_percent=round(100.0 * (total_time - lower_bound) / lower_bound, 6),
        runtime_seconds=round(time.perf_counter() - started, 6),
        conflict_count=conflicts,
        candidate_variants=sum(len(values) for values in variants.values()),
        failed_person_id=None,
    )
    return flights, stats


def _flight_accepts_person(
    flight: Q3Flight,
    person: Q3Person,
    people: dict[str, Q3Person],
    config: ProblemConfig,
) -> tuple[int, int] | None:
    assignment = _assignment_for_person(person, flight.variant, config)
    if assignment is None:
        return None
    pickup, delivery = assignment
    if (
        flight.departures[pickup] < person.earliest
        or flight.arrivals[delivery] > person.latest
    ):
        return None
    loads = [0] * (len(flight.variant.source.route.stops) - 1)
    for existing_pickup, existing_delivery in flight.assignment_intervals.values():
        for leg in range(existing_pickup, existing_delivery):
            loads[leg] += 1
    if any(loads[leg] >= flight.variant.capacity for leg in range(pickup, delivery)):
        return None
    return assignment


def insert_optional_people(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    config: ProblemConfig,
) -> tuple[list[Q3Flight], list[str]]:
    flights = deepcopy(list(baseline))
    optional = sorted(
        (person for person in people.values() if not person.mandatory),
        key=lambda person: (person.latest, person.latest - person.earliest, person.person_id),
    )
    unserved: list[str] = []
    for person in optional:
        choices = [
            (flight, assignment)
            for flight in flights
            if (assignment := _flight_accepts_person(flight, person, people, config)) is not None
        ]
        if choices:
            flight, assignment = min(
                choices,
                key=lambda item: (
                    item[0].variant.capacity - len(item[0].person_ids),
                    item[0].arrivals[item[1][1]]
                    - item[0].departures[item[1][0]],
                    item[0].start,
                    item[0].aircraft_id,
                ),
            )
            flight.person_ids.append(person.person_id)
            flight.assignment_intervals[person.person_id] = assignment
            continue
        unserved.append(person.person_id)

    # One-person relocation repair can create a seat for a remaining optional
    # passenger without changing any route, time, aircraft or T0.
    still_unserved: list[str] = []
    for person_id in unserved:
        person = people[person_id]
        repaired = False
        full_targets = [
            flight
            for flight in flights
            if (assignment := _assignment_for_person(person, flight.variant, config)) is not None
            and flight.departures[assignment[0]] >= person.earliest
            and flight.arrivals[assignment[1]] <= person.latest
        ]
        for target in full_targets:
            for moved_id in list(target.person_ids):
                moved = people[moved_id]
                destinations = [
                    (flight, assignment)
                    for flight in flights
                    if flight is not target
                    and (
                        assignment := _flight_accepts_person(
                            flight, moved, people, config
                        )
                    )
                    is not None
                ]
                if not destinations:
                    continue
                destination, moved_assignment = min(
                    destinations, key=lambda item: len(item[0].person_ids)
                )
                # Remove the relocated passenger provisionally, then run the
                # same leg-by-leg capacity and time-window gate used by normal
                # insertion.  A route can be full only on a subset of its legs,
                # so a raw passenger-count test is not sufficient here.
                target.person_ids.remove(moved_id)
                old_assignment = target.assignment_intervals.pop(moved_id)
                optional_assignment = _flight_accepts_person(
                    target, person, people, config
                )
                if optional_assignment is None:
                    target.person_ids.append(moved_id)
                    target.assignment_intervals[moved_id] = old_assignment
                    continue
                destination.person_ids.append(moved_id)
                destination.assignment_intervals[moved_id] = moved_assignment
                target.person_ids.append(person_id)
                target.assignment_intervals[person_id] = optional_assignment
                repaired = True
                break
            if repaired:
                break
        if not repaired:
            still_unserved.append(person_id)
    return flights, still_unserved


def optimize_fixed_flight_assignments(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    config: ProblemConfig,
    *,
    time_limit_seconds: float = 90.0,
) -> tuple[list[Q3Flight], list[str], dict[str, object]]:
    """Globally reassign people on fixed flights with a sparse binary MILP.

    The flight routes, start times and aircraft remain unchanged, hence the
    stage-one aircraft-time upper bound is preserved exactly.  The first solve
    maximizes served temporary people; the second fixes that cardinality and
    minimizes total passenger time.  Mandatory people retain equality cover
    constraints and every flight leg has its own seat-capacity row.
    """
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix, vstack

    flights = deepcopy(list(baseline))
    options: list[tuple[str, int, tuple[int, int], int]] = []
    person_columns: dict[str, list[int]] = defaultdict(list)
    leg_columns: dict[tuple[int, int], list[int]] = defaultdict(list)
    for person_id in sorted(people):
        person = people[person_id]
        for flight_index, flight in enumerate(flights):
            assignment = _assignment_for_person(person, flight.variant, config)
            if assignment is None:
                continue
            pickup, delivery = assignment
            if (
                flight.departures[pickup] < person.earliest
                or flight.arrivals[delivery] > person.latest
            ):
                continue
            passenger_time = (
                flight.arrivals[delivery] - flight.departures[pickup]
            )
            column = len(options)
            options.append((person_id, flight_index, assignment, passenger_time))
            person_columns[person_id].append(column)
            for leg in range(pickup, delivery):
                leg_columns[(flight_index, leg)].append(column)

    missing = [
        person_id
        for person_id, person in people.items()
        if person.mandatory and not person_columns.get(person_id)
    ]
    if missing:
        raise RuntimeError(f"Fixed-flight MILP has no option for mandatory people: {missing[:8]}")

    row_indices: list[int] = []
    column_indices: list[int] = []
    values: list[float] = []
    lower: list[float] = []
    upper: list[float] = []
    row = 0
    for person_id in sorted(people):
        for column in person_columns.get(person_id, []):
            row_indices.append(row)
            column_indices.append(column)
            values.append(1.0)
        if people[person_id].mandatory:
            lower.append(1.0)
            upper.append(1.0)
        else:
            lower.append(0.0)
            upper.append(1.0)
        row += 1
    for (flight_index, _leg), columns in sorted(leg_columns.items()):
        for column in columns:
            row_indices.append(row)
            column_indices.append(column)
            values.append(1.0)
        lower.append(0.0)
        upper.append(float(flights[flight_index].variant.capacity))
        row += 1
    matrix = coo_matrix(
        (values, (row_indices, column_indices)),
        shape=(row, len(options)),
    ).tocsr()
    constraints = LinearConstraint(matrix, np.asarray(lower), np.asarray(upper))
    integrality = np.ones(len(options), dtype=int)
    bounds = Bounds(np.zeros(len(options)), np.ones(len(options)))

    temporary_columns = [
        column
        for column, (person_id, _, _, _) in enumerate(options)
        if not people[person_id].mandatory
    ]
    first_objective = np.zeros(len(options))
    first_objective[temporary_columns] = -1.0
    first = milp(
        first_objective,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
        options={"time_limit": float(time_limit_seconds)},
    )
    if first.x is None:
        raise RuntimeError(f"Fixed-flight MILP failed: status={first.status}, {first.message}")
    served_temporary = int(
        round(sum(first.x[column] for column in temporary_columns))
    )

    # Lexicographic second pass: keep the maximum temporary count and reduce
    # passenger time.  If the solver stops early, the first-pass incumbent is
    # retained so feasibility and the service count can never regress.
    temp_row = coo_matrix(
        (
            np.ones(len(temporary_columns)),
            (np.zeros(len(temporary_columns)), temporary_columns),
        ),
        shape=(1, len(options)),
    ).tocsr()
    second_constraints = LinearConstraint(
        vstack([matrix, temp_row], format="csr"),
        np.append(np.asarray(lower), float(served_temporary)),
        np.append(np.asarray(upper), float(served_temporary)),
    )
    passenger_objective = np.asarray(
        [float(option[3]) for option in options], dtype=float
    )
    second = milp(
        passenger_objective,
        integrality=integrality,
        bounds=bounds,
        constraints=second_constraints,
        options={"time_limit": float(time_limit_seconds)},
    )
    chosen = second.x if second.x is not None else first.x

    for flight in flights:
        flight.person_ids.clear()
        flight.assignment_intervals.clear()
    assigned: set[str] = set()
    for column, value in enumerate(chosen):
        if value < 0.5:
            continue
        person_id, flight_index, assignment, _ = options[column]
        flights[flight_index].person_ids.append(person_id)
        flights[flight_index].assignment_intervals[person_id] = assignment
        assigned.add(person_id)
    unserved = sorted(
        person_id
        for person_id, person in people.items()
        if not person.mandatory and person_id not in assigned
    )
    stats = {
        "variables": len(options),
        "constraints": int(matrix.shape[0]),
        "stage1_status": int(first.status),
        "stage1_message": str(first.message),
        "stage2_status": int(second.status),
        "stage2_message": str(second.message),
        "served_temporary": len(temporary_columns) and len(
            [person_id for person_id in people if not people[person_id].mandatory]
        ) - len(unserved),
        "temporary_upper_bound": sum(not person.mandatory for person in people.values()),
        "fixed_flight_optimal": int(first.status) == 0,
        "all_optional_served": not unserved,
    }
    return flights, unserved, stats


def shorten_fixed_flight_routes(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    config: ProblemConfig,
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Replace individual flights by shorter compatible cached variants.

    Passenger membership and the concrete aircraft are fixed.  Each proposed
    replacement is rechecked for pickup order, every-leg capacity, the common
    time window and neighbouring flights (including the 30-minute turnaround).
    Hence every accepted move monotonically reduces aircraft time without
    sacrificing any served person.
    """
    flights = deepcopy(list(baseline))
    unique = {
        variant.key: variant
        for values in variants_by_od.values()
        for variant in values
    }
    pools: dict[tuple[str, str], list[Q3Variant]] = defaultdict(list)
    for variant in unique.values():
        pools[(variant.base_airport, variant.aircraft_type)].append(variant)
    for values in pools.values():
        values.sort(key=lambda variant: (variant.duration, variant.fuel_kg, variant.key))

    calendars: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for flight in flights:
        calendars[flight.aircraft_id].append((flight.start, flight.end))
    for values in calendars.values():
        values.sort()

    replacements: list[dict[str, object]] = []
    # Long flights first: route pruning there has the largest potential gain.
    for flight in sorted(flights, key=lambda item: (-item.duration, item.start)):
        current_interval = (flight.start, flight.end)
        aircraft_calendar = calendars[flight.aircraft_id]
        aircraft_calendar.remove(current_interval)
        day = flight.start // 1440
        best: tuple[
            tuple[object, ...], Q3Variant, int, dict[str, tuple[int, int]]
        ] | None = None
        pool = pools[(flight.variant.base_airport, flight.variant.aircraft_type)]
        for candidate in pool:
            if candidate.duration >= flight.duration:
                break
            assignments: dict[str, tuple[int, int]] = {}
            loads = [0] * (len(candidate.source.route.stops) - 1)
            lower = day * 1440 + 360
            upper = min(day * 1440 + 1080, day * 1440 + 1200 - candidate.duration)
            passenger_time = 0
            feasible = True
            for person_id in flight.person_ids:
                person = people[person_id]
                assignment = _assignment_for_person(person, candidate, config)
                if assignment is None:
                    feasible = False
                    break
                interval = next(
                    (
                        (lo, hi)
                        for q, lo, hi in _person_day_intervals(
                            person, candidate, config, assignment
                        )
                        if q == day
                    ),
                    None,
                )
                if interval is None:
                    feasible = False
                    break
                lower = max(lower, interval[0])
                upper = min(upper, interval[1])
                if lower > upper:
                    feasible = False
                    break
                pickup, delivery = assignment
                for leg in range(pickup, delivery):
                    loads[leg] += 1
                    if loads[leg] > candidate.capacity:
                        feasible = False
                        break
                if not feasible:
                    break
                passenger_time += (
                    candidate.source.arrivals[delivery]
                    - candidate.source.departures[pickup]
                )
                assignments[person_id] = assignment
            if not feasible:
                continue
            start = _first_calendar_slot(
                aircraft_calendar,
                lower,
                upper,
                candidate.duration,
                config.turnaround_minutes,
            )
            if start is None:
                continue
            score = (candidate.duration, passenger_time, candidate.fuel_kg, start)
            if best is None or score < best[0]:
                best = (score, candidate, start, assignments)
        if best is None:
            aircraft_calendar.append(current_interval)
            aircraft_calendar.sort()
            continue
        _, candidate, start, assignments = best
        old_duration = flight.duration
        old_key = flight.variant.key
        flight.variant = candidate
        flight.start = start
        flight.assignment_intervals = assignments
        flight.timing = None
        aircraft_calendar.append((flight.start, flight.end))
        aircraft_calendar.sort()
        replacements.append(
            {
                "aircraft_id": flight.aircraft_id,
                "old_duration": old_duration,
                "new_duration": candidate.duration,
                "saved_minutes": old_duration - candidate.duration,
                "old_route_key": repr(old_key),
                "new_route_key": repr(candidate.key),
            }
        )
    return flights, {
        "replacement_count": len(replacements),
        "saved_aircraft_time_minutes": sum(
            int(row["saved_minutes"]) for row in replacements
        ),
        "replacements": replacements,
    }


def retype_and_rehome_flights(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    config: ProblemConfig,
    *,
    maximum_passes: int = 3,
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Local search over route, aircraft type, home airport and start time.

    A flight's passenger set is fixed during one move, but every cached route
    and every compatible concrete aircraft may be used.  LAND endpoints are
    therefore free to select any legal airport, while explicit facility/airport
    endpoints remain protected by ``assignment_interval``.  Only strictly
    shorter, fully feasible moves are accepted.
    """
    flights = deepcopy(list(baseline))
    unique = sorted(
        {
            variant.key: variant
            for values in variants_by_od.values()
            for variant in values
        }.values(),
        key=lambda variant: (variant.duration, variant.fuel_kg, variant.key),
    )
    calendars: dict[str, list[tuple[int, int]]] = {
        aircraft_id: [] for aircraft_id in config.fleet_ids
    }
    for flight in flights:
        calendars[flight.aircraft_id].append((flight.start, flight.end))
    for values in calendars.values():
        values.sort()

    moves: list[dict[str, object]] = []
    for pass_index in range(maximum_passes):
        pass_moves = 0
        for flight in sorted(flights, key=lambda item: (-item.duration, item.start)):
            old_aircraft = flight.aircraft_id
            old_interval = (flight.start, flight.end)
            calendars[old_aircraft].remove(old_interval)
            day = flight.start // 1440
            best: tuple[
                tuple[object, ...],
                Q3Variant,
                str,
                int,
                dict[str, tuple[int, int]],
            ] | None = None
            for candidate in unique:
                if candidate.duration >= flight.duration:
                    break
                assignments: dict[str, tuple[int, int]] = {}
                loads = [0] * (len(candidate.source.route.stops) - 1)
                lower = day * 1440 + 360
                upper = min(
                    day * 1440 + 1080,
                    day * 1440 + 1200 - candidate.duration,
                )
                passenger_time = 0
                feasible = True
                for person_id in flight.person_ids:
                    person = people[person_id]
                    assignment = _assignment_for_person(person, candidate, config)
                    if assignment is None:
                        feasible = False
                        break
                    interval = next(
                        (
                            (lo, hi)
                            for q, lo, hi in _person_day_intervals(
                                person, candidate, config, assignment
                            )
                            if q == day
                        ),
                        None,
                    )
                    if interval is None:
                        feasible = False
                        break
                    lower = max(lower, interval[0])
                    upper = min(upper, interval[1])
                    if lower > upper:
                        feasible = False
                        break
                    pickup, delivery = assignment
                    for leg in range(pickup, delivery):
                        loads[leg] += 1
                        if loads[leg] > candidate.capacity:
                            feasible = False
                            break
                    if not feasible:
                        break
                    passenger_time += (
                        candidate.source.arrivals[delivery]
                        - candidate.source.departures[pickup]
                    )
                    assignments[person_id] = assignment
                if not feasible:
                    continue
                fleet = _fleet_slot(calendars, config, candidate, lower, upper)
                if fleet is None:
                    continue
                aircraft_id, start = fleet
                score = (
                    candidate.duration,
                    passenger_time,
                    candidate.fuel_kg,
                    start,
                    aircraft_id,
                )
                if best is None or score < best[0]:
                    best = (score, candidate, aircraft_id, start, assignments)
            if best is None:
                calendars[old_aircraft].append(old_interval)
                calendars[old_aircraft].sort()
                continue
            _, candidate, aircraft_id, start, assignments = best
            old_duration = flight.duration
            old_base = flight.variant.base_airport
            old_type = flight.variant.aircraft_type
            flight.variant = candidate
            flight.aircraft_id = aircraft_id
            flight.start = start
            flight.assignment_intervals = assignments
            flight.timing = None
            calendars[aircraft_id].append((flight.start, flight.end))
            calendars[aircraft_id].sort()
            pass_moves += 1
            moves.append(
                {
                    "pass": pass_index + 1,
                    "old_aircraft_id": old_aircraft,
                    "new_aircraft_id": aircraft_id,
                    "old_base_airport": old_base,
                    "new_base_airport": candidate.base_airport,
                    "old_aircraft_type": old_type,
                    "new_aircraft_type": candidate.aircraft_type,
                    "old_duration": old_duration,
                    "new_duration": candidate.duration,
                    "saved_minutes": old_duration - candidate.duration,
                }
            )
        if pass_moves == 0:
            break
    return flights, {
        "move_count": len(moves),
        "saved_aircraft_time_minutes": sum(int(move["saved_minutes"]) for move in moves),
        "moves": moves,
    }


def destroy_repair_route_descent(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    config: ProblemConfig,
    *,
    minimum_optional_served: int,
    maximum_trials: int = 30,
    assignment_time_limit_seconds: float = 30.0,
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Prune route stops and globally repair all passenger assignments.

    Unlike ``shorten_fixed_flight_routes``, this neighbourhood may temporarily
    remove a stop used by the flight's current passengers.  A sparse assignment
    MILP then reallocates *all* people over the complete flight set.  A move is
    accepted only when every mandatory person remains covered, the requested
    optional service level is preserved and aircraft time strictly decreases.
    """
    incumbent = deepcopy(list(baseline))
    unique = {
        variant.key: variant
        for values in variants_by_od.values()
        for variant in values
    }
    pools: dict[tuple[str, str], list[Q3Variant]] = defaultdict(list)
    for variant in unique.values():
        pools[(variant.base_airport, variant.aircraft_type)].append(variant)
    moves: list[dict[str, object]] = []
    trials = 0
    improved = True
    while improved and trials < maximum_trials:
        improved = False
        proposals: list[tuple[int, int, Q3Variant]] = []
        for index, flight in enumerate(incumbent):
            current_facilities = {
                stop.facility_id
                for stop in flight.variant.source.route.stops[1:-1]
            }
            for candidate in pools[
                (flight.variant.base_airport, flight.variant.aircraft_type)
            ]:
                if candidate.duration >= flight.duration:
                    continue
                candidate_facilities = {
                    stop.facility_id
                    for stop in candidate.source.route.stops[1:-1]
                }
                if not candidate_facilities or not candidate_facilities.issubset(
                    current_facilities
                ):
                    continue
                proposals.append(
                    (flight.duration - candidate.duration, index, candidate)
                )
        proposals.sort(
            key=lambda item: (-item[0], item[2].duration, item[1], item[2].key)
        )
        for saving, index, candidate in proposals:
            if trials >= maximum_trials:
                break
            trials += 1
            trial = deepcopy(incumbent)
            old = trial[index]
            old_duration = old.duration
            old_key = old.variant.key
            old.variant = candidate
            old.person_ids.clear()
            old.assignment_intervals.clear()
            old.timing = None
            try:
                repaired, unserved, milp_stats = optimize_fixed_flight_assignments(
                    trial,
                    people,
                    config,
                    time_limit_seconds=assignment_time_limit_seconds,
                )
            except RuntimeError:
                continue
            served_optional = sum(
                1
                for flight in repaired
                for person_id in flight.person_ids
                if not people[person_id].mandatory
            )
            if served_optional < minimum_optional_served:
                continue
            incumbent = repaired
            moves.append(
                {
                    "flight_index": index,
                    "old_duration": old_duration,
                    "new_duration": candidate.duration,
                    "saved_minutes": saving,
                    "old_route_key": repr(old_key),
                    "new_route_key": repr(candidate.key),
                    "served_optional": served_optional,
                    "assignment_milp": milp_stats,
                    "unserved_optional": unserved,
                }
            )
            improved = True
            break
    return incumbent, {
        "trial_count": trials,
        "accepted_move_count": len(moves),
        "saved_aircraft_time_minutes": sum(int(move["saved_minutes"]) for move in moves),
        "moves": moves,
    }


def multiflight_ruin_recreate_descent(
    baseline: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    config: ProblemConfig,
    *,
    minimum_optional_served: int = 0,
    maximum_trials: int = 50,
    maximum_neighbors: int = 8,
    route_limit: int = 100,
    assignment_time_limit_seconds: float = 20.0,
) -> tuple[list[Q3Flight], dict[str, object]]:
    """Same-day 2-to-1 ruin-and-recreate over the cached route pool.

    This is the first structural P1-C neighbourhood: two related old flights
    may be replaced by one route, concrete aircraft and time-aware timetable.
    A global fixed-flight assignment MILP is then used as a repair step.  Only
    strict aircraft-time improvements preserving mandatory and optional service
    are accepted.
    """

    started = time.perf_counter()
    incumbent = deepcopy(list(baseline))
    required_mandatory = sum(person.mandatory for person in people.values())
    rejections: Counter[str] = Counter()
    accepted: list[dict[str, object]] = []
    trials = 0

    def facilities(flight: Q3Flight) -> frozenset[str]:
        return frozenset(
            stop.facility_id for stop in flight.variant.source.route.stops[1:-1]
        )

    while trials < maximum_trials:
        pair_scores: list[tuple[float, int, int]] = []
        for left, flight in enumerate(incumbent):
            candidates: list[tuple[float, int]] = []
            left_facilities = facilities(flight)
            for right, other in enumerate(incumbent):
                if right <= left or other.start // 1440 != flight.start // 1440:
                    continue
                right_facilities = facilities(other)
                union = left_facilities | right_facilities
                jaccard = len(left_facilities & right_facilities) / max(1, len(union))
                same_base = flight.variant.base_airport == other.variant.base_airport
                time_proximity = max(0.0, 1.0 - abs(flight.start - other.start) / 720.0)
                score = 2.0 * jaccard + float(same_base) + 0.25 * time_proximity
                candidates.append((-score, right))
            for negative_score, right in sorted(candidates)[:maximum_neighbors]:
                pair_scores.append((negative_score, left, right))
        if not pair_scores:
            break
        improved = False
        for _negative_score, left, right in sorted(set(pair_scores)):
            if trials >= maximum_trials:
                break
            trials += 1
            selected = (incumbent[left], incumbent[right])
            day = selected[0].start // 1440
            ruined_ids = sorted(
                {person_id for flight in selected for person_id in flight.person_ids}
            )
            if not ruined_ids:
                rejections["empty_neighborhood"] += 1
                continue
            common_keys: set[tuple[object, ...]] | None = None
            key_to_variant: dict[tuple[object, ...], Q3Variant] = {}
            for person_id in ruined_ids:
                od_variants = variants_by_od[people[person_id].od]
                keys = {variant.key for variant in od_variants}
                key_to_variant.update({variant.key: variant for variant in od_variants})
                common_keys = keys if common_keys is None else common_keys & keys
                if not common_keys:
                    break
            if not common_keys:
                rejections["no_route_candidates"] += 1
                continue
            old_time = sum(flight.duration for flight in selected)
            candidates = sorted(
                (key_to_variant[key] for key in common_keys),
                key=lambda variant: (variant.duration, -variant.capacity, variant.key),
            )[:route_limit]
            unaffected = [
                deepcopy(flight)
                for index, flight in enumerate(incumbent)
                if index not in {left, right}
            ]
            calendars: dict[str, list[tuple[int, int]]] = defaultdict(list)
            for flight in unaffected:
                calendars[flight.aircraft_id].append((flight.start, flight.end))
            for values in calendars.values():
                values.sort()

            best: tuple[
                tuple[object, ...], Q3Variant, str, Q3FlightTiming, dict[str, tuple[int, int]]
            ] | None = None
            for candidate in candidates:
                if candidate.duration >= old_time:
                    break
                assignments: dict[str, tuple[int, int]] = {}
                loads = [0] * (len(candidate.source.route.stops) - 1)
                feasible = True
                for person_id in ruined_ids:
                    assignment = _assignment_for_person(
                        people[person_id], candidate, config
                    )
                    if assignment is None:
                        feasible = False
                        break
                    pickup, delivery = assignment
                    for leg in range(pickup, delivery):
                        loads[leg] += 1
                        if loads[leg] > candidate.capacity:
                            feasible = False
                            break
                    if not feasible:
                        break
                    assignments[person_id] = assignment
                if not feasible:
                    continue
                for aircraft_id in _compatible_aircraft(config, candidate):
                    event_uppers = {day * 1440 + 1080}
                    for start, _end in calendars[aircraft_id]:
                        if start // 1440 == day:
                            event_uppers.add(start - config.turnaround_minutes)
                    for upper in sorted(event_uppers, reverse=True):
                        timing = schedule_route_timing(
                            candidate,
                            assignments,
                            people,
                            day,
                            config,
                            start_upper=upper,
                        )
                        if timing is None:
                            continue
                        conflict = any(
                            not (
                                timing.arrivals[-1] + config.turnaround_minutes <= start
                                or end + config.turnaround_minutes <= timing.departures[0]
                            )
                            for start, end in calendars[aircraft_id]
                        )
                        if conflict:
                            continue
                        score = (
                            timing.duration,
                            sum(timing.waiting_minutes),
                            timing.departures[0],
                            candidate.fuel_kg,
                            aircraft_id,
                        )
                        if best is None or score < best[0]:
                            best = (score, candidate, aircraft_id, timing, assignments)
            if best is None:
                rejections["no_concrete_option"] += 1
                continue
            _score, candidate, aircraft_id, timing, assignments = best
            replacement = Q3Flight(
                variant=candidate,
                aircraft_id=aircraft_id,
                start=timing.departures[0],
                person_ids=list(ruined_ids),
                assignment_intervals=assignments,
                timing=timing,
            )
            trial = unaffected + [replacement]
            try:
                repaired, _unserved, milp_stats = optimize_fixed_flight_assignments(
                    trial,
                    people,
                    config,
                    time_limit_seconds=assignment_time_limit_seconds,
                )
            except RuntimeError:
                rejections["assignment_milp"] += 1
                continue
            repaired = [flight for flight in repaired if flight.person_ids]
            metrics = schedule_metrics(repaired, people)
            if int(metrics["served_mandatory"]) != required_mandatory:
                rejections["mandatory_coverage"] += 1
                continue
            if int(metrics["served_optional"]) < minimum_optional_served:
                rejections["optional_service"] += 1
                continue
            before = schedule_metrics(incumbent, people)
            if int(metrics["total_aircraft_time_minutes"]) >= int(
                before["total_aircraft_time_minutes"]
            ):
                rejections["no_saving"] += 1
                continue
            incumbent = repaired
            accepted.append(
                {
                    "move": "2->1",
                    "old_aircraft_time_minutes": before[
                        "total_aircraft_time_minutes"
                    ],
                    "new_aircraft_time_minutes": metrics[
                        "total_aircraft_time_minutes"
                    ],
                    "saved_minutes": int(before["total_aircraft_time_minutes"])
                    - int(metrics["total_aircraft_time_minutes"]),
                    "old_flights": before["total_flights"],
                    "new_flights": metrics["total_flights"],
                    "waiting_minutes": sum(timing.waiting_minutes),
                    "assignment_milp": milp_stats,
                }
            )
            improved = True
            break
        if not improved:
            break
    return incumbent, {
        "trials": trials,
        "accepted_count": len(accepted),
        "two_to_one_count": sum(move["move"] == "2->1" for move in accepted),
        "saved_aircraft_time_minutes": sum(
            int(move["saved_minutes"]) for move in accepted
        ),
        "runtime_seconds": round(time.perf_counter() - started, 6),
        "rejection_reasons": dict(sorted(rejections.items())),
        "moves": accepted,
    }


def _timestamp(config: ProblemConfig, minutes: int) -> str:
    return (config.planning_start + timedelta(minutes=minutes)).strftime(TIME_FORMAT)


def project_mandatory_only(
    flights: Sequence[Q3Flight], people: dict[str, Q3Person]
) -> list[Q3Flight]:
    """Strictly project a schedule by deleting optional assignments only."""

    projected = deepcopy(list(flights))
    for flight in projected:
        keep = [person_id for person_id in flight.person_ids if people[person_id].mandatory]
        flight.person_ids = keep
        flight.assignment_intervals = {
            person_id: flight.assignment_intervals[person_id] for person_id in keep
        }
    return projected


def schedule_metrics(
    flights: Sequence[Q3Flight], people: dict[str, Q3Person]
) -> dict[str, object]:
    assigned = {
        person_id for flight in flights for person_id in flight.person_ids
    }
    passenger_time = sum(
        flight.arrivals[delivery] - flight.departures[pickup]
        for flight in flights
        for pickup, delivery in flight.assignment_intervals.values()
    )
    return {
        "total_aircraft_time_minutes": sum(flight.duration for flight in flights),
        "total_passenger_travel_time_minutes": passenger_time,
        "total_flights": len(flights),
        "total_fuel_consumption_kg": round(
            sum(flight.variant.fuel_kg for flight in flights), 6
        ),
        "served_mandatory": sum(
            person.mandatory and person_id in assigned
            for person_id, person in people.items()
        ),
        "served_optional": sum(
            (not person.mandatory) and person_id in assigned
            for person_id, person in people.items()
        ),
    }


def schedule_comparison_key(
    flights: Sequence[Q3Flight], people: dict[str, Q3Person], *, stage: int = 1
) -> tuple[float, ...]:
    metrics = schedule_metrics(flights, people)
    base = (
        float(metrics["total_aircraft_time_minutes"]),
        float(metrics["total_passenger_travel_time_minutes"]),
        float(metrics["total_flights"]),
        float(metrics["total_fuel_consumption_kg"]),
    )
    if stage == 2:
        return (-float(metrics["served_optional"]),) + base
    return base


def load_q3_schedule(
    routes_path: Path,
    assignments_path: Path,
    people: dict[str, Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    config: ProblemConfig,
) -> list[Q3Flight]:
    """Reconstruct and verifyable in-memory flights from exported Q3 CSV files."""

    unique = {
        variant.key: variant
        for values in variants_by_od.values()
        for variant in values
    }
    by_signature: dict[
        tuple[str, str, tuple[tuple[str, int], ...]], list[Q3Variant]
    ] = defaultdict(list)
    for variant in unique.values():
        signature = (
            variant.base_airport,
            variant.aircraft_type,
            tuple(
                (stop.facility_id, int(stop.refuel))
                for stop in variant.source.route.stops
            ),
        )
        by_signature[signature].append(variant)

    with routes_path.open("r", encoding="utf-8-sig", newline="") as stream:
        route_rows = list(csv.DictReader(stream))
    grouped: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in route_rows:
        grouped[(row["aircraft_id"], int(row["flight_no"]))].append(row)

    flights: list[Q3Flight] = []
    flight_lookup: dict[tuple[str, int], Q3Flight] = {}
    for key, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: int(row["stop_order"]))
        aircraft_id, flight_no = key
        base, aircraft_type, _tail = aircraft_id.split("-", 2)
        signature = (
            base,
            aircraft_type,
            tuple((row["facility_id"], int(row["refuel"])) for row in rows),
        )
        matches = by_signature.get(signature, [])
        if not matches:
            raise ValueError(f"No cached Q3 variant matches exported flight {key}")
        variant = min(matches, key=lambda item: item.key)
        start_dt = datetime.strptime(rows[0]["departure_time"], TIME_FORMAT)
        start = round((start_dt - config.planning_start).total_seconds() / 60)
        arrivals: list[int] = [start]
        departures: list[int] = [start]
        for index, row in enumerate(rows[1:], start=1):
            arrival_dt = datetime.strptime(row["arrival_time"], TIME_FORMAT)
            arrival = round((arrival_dt - config.planning_start).total_seconds() / 60)
            arrivals.append(arrival)
            if index == len(rows) - 1:
                departures.append(arrival)
            else:
                departure_dt = datetime.strptime(row["departure_time"], TIME_FORMAT)
                departures.append(
                    round((departure_dt - config.planning_start).total_seconds() / 60)
                )
        timing = Q3FlightTiming(
            arrivals=tuple(arrivals),
            departures=tuple(departures),
            duration=arrivals[-1] - start,
            waiting_minutes=tuple(0 for _ in rows),
        )
        flight = Q3Flight(
            variant=variant,
            aircraft_id=aircraft_id,
            start=start,
            flight_no=flight_no,
            timing=timing,
        )
        flights.append(flight)
        flight_lookup[key] = flight

    with assignments_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if not row["aircraft_id"]:
                continue
            person_id = row["person_id"]
            if person_id not in people:
                raise ValueError(f"Unknown person in Q3 assignment CSV: {person_id}")
            key = (row["aircraft_id"], int(row["flight_no"]))
            flight = flight_lookup[key]
            interval = (
                int(row["pickup_stop_order"]),
                int(row["delivery_stop_order"]),
            )
            flight.person_ids.append(person_id)
            flight.assignment_intervals[person_id] = interval
    return flights


def export_q3_schedule(
    flights: Sequence[Q3Flight],
    people: dict[str, Q3Person],
    routes_path: Path,
    assignments_path: Path,
    config: ProblemConfig,
) -> None:
    ordered = sorted(flights, key=lambda flight: (flight.aircraft_id, flight.start, flight.end))
    by_aircraft: Counter[str] = Counter()
    assignment_lookup: dict[str, tuple[str, int, int, int]] = {}
    route_rows: list[dict[str, object]] = []
    for flight in ordered:
        by_aircraft[flight.aircraft_id] += 1
        flight.flight_no = by_aircraft[flight.aircraft_id]
        stops = flight.variant.source.route.stops
        for stop_order, stop in enumerate(stops):
            arrival = "" if stop_order == 0 else _timestamp(
                config, flight.arrivals[stop_order]
            )
            departure = "" if stop_order == len(stops) - 1 else _timestamp(
                config, flight.departures[stop_order]
            )
            route_rows.append(
                {
                    "aircraft_id": flight.aircraft_id,
                    "flight_no": flight.flight_no,
                    "stop_order": stop_order,
                    "facility_id": stop.facility_id,
                    "arrival_time": arrival,
                    "departure_time": departure,
                    "refuel": int(stop.refuel),
                }
            )
        for person_id in flight.person_ids:
            pickup, delivery = flight.assignment_intervals[person_id]
            assignment_lookup[person_id] = (
                flight.aircraft_id,
                flight.flight_no,
                pickup,
                delivery,
            )
    assignment_rows: list[dict[str, object]] = []
    for person_id in sorted(people):
        value = assignment_lookup.get(person_id)
        if value is None:
            assignment_rows.append(
                {
                    "person_id": person_id,
                    "aircraft_id": "",
                    "flight_no": "",
                    "pickup_stop_order": "",
                    "delivery_stop_order": "",
                }
            )
        else:
            aircraft_id, flight_no, pickup, delivery = value
            assignment_rows.append(
                {
                    "person_id": person_id,
                    "aircraft_id": aircraft_id,
                    "flight_no": flight_no,
                    "pickup_stop_order": pickup,
                    "delivery_stop_order": delivery,
                }
            )
    write_csv(
        routes_path,
        [
            "aircraft_id",
            "flight_no",
            "stop_order",
            "facility_id",
            "arrival_time",
            "departure_time",
            "refuel",
        ],
        route_rows,
    )
    write_csv(
        assignments_path,
        [
            "person_id",
            "aircraft_id",
            "flight_no",
            "pickup_stop_order",
            "delivery_stop_order",
        ],
        assignment_rows,
    )


def schedule_task_counts(flights: Sequence[Q3Flight], people: dict[str, Q3Person]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for flight in flights:
        counts.update(people[person_id].task_type for person_id in flight.person_ids)
    return dict(sorted(counts.items()))
