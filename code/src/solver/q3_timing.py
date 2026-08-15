from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from ..config import ProblemConfig
from ..rules import minimum_stop_minutes

if TYPE_CHECKING:
    from .q3 import Q3Person, Q3Variant


@dataclass(frozen=True)
class Q3FlightTiming:
    """Absolute integer-minute timing for one Q3 flight."""

    arrivals: tuple[int, ...]
    departures: tuple[int, ...]
    duration: int
    waiting_minutes: tuple[int, ...]


def schedule_route_timing(
    variant: "Q3Variant",
    assignments: Mapping[str, tuple[int, int]],
    people: Mapping[str, "Q3Person"],
    day: int,
    config: ProblemConfig,
    *,
    start_lower: int | None = None,
    start_upper: int | None = None,
) -> Q3FlightTiming | None:
    """Return the minimum-duration legal timing, allowing offshore waiting.

    For a fixed departure minute the earliest feasible downstream timetable is
    obtained by forward propagation.  Delaying the departure never increases
    required waiting, so scanning feasible departure minutes from latest to
    earliest returns a minimum-duration integer timetable with a deterministic
    latest-start tie break.  A route has at most five offshore stops, making
    the at-most-721-minute scan inexpensive and easier to audit than a generic
    LP in this high-frequency evaluator.
    """

    if not 0 <= day < 7:
        return None
    stops = variant.source.route.stops
    stop_count = len(stops)
    if stop_count < 2:
        return None

    day_offset = 1440 * day
    lower = max(day_offset + 360, start_lower if start_lower is not None else -10**9)
    upper = min(day_offset + 1080, start_upper if start_upper is not None else 10**9)

    pickup_earliest: list[int | None] = [None] * stop_count
    delivery_latest: list[int | None] = [None] * stop_count
    for person_id, (pickup, delivery) in assignments.items():
        person = people[person_id]
        if not 0 <= pickup < delivery < stop_count:
            return None
        pickup_earliest[pickup] = max(
            pickup_earliest[pickup] if pickup_earliest[pickup] is not None else -10**9,
            person.earliest,
        )
        delivery_latest[delivery] = min(
            delivery_latest[delivery] if delivery_latest[delivery] is not None else 10**9,
            person.latest,
        )
    if pickup_earliest[0] is not None:
        lower = max(lower, pickup_earliest[0])
    if lower > upper:
        return None

    travel = tuple(
        variant.source.arrivals[index + 1] - variant.source.departures[index]
        for index in range(stop_count - 1)
    )
    minimum_dwell = [0] * stop_count
    for index in range(1, stop_count - 1):
        stop = stops[index]
        minimum_dwell[index] = minimum_stop_minutes(
            stop.facility_id, stop.refuel, config
        )

    for start in range(upper, lower - 1, -1):
        arrivals = [start] * stop_count
        departures = [start] * stop_count
        waiting = [0] * stop_count
        feasible = True
        for index in range(stop_count - 1):
            arrival = departures[index] + travel[index]
            arrivals[index + 1] = arrival
            latest = delivery_latest[index + 1]
            if latest is not None and arrival > latest:
                feasible = False
                break
            if index + 1 == stop_count - 1:
                departures[index + 1] = arrival
                continue
            departure = arrival + minimum_dwell[index + 1]
            earliest = pickup_earliest[index + 1]
            if earliest is not None and departure < earliest:
                departure = earliest
            departures[index + 1] = departure
            waiting[index + 1] = departure - arrival - minimum_dwell[index + 1]
        if not feasible or arrivals[-1] > day_offset + 1200:
            continue
        return Q3FlightTiming(
            arrivals=tuple(arrivals),
            departures=tuple(departures),
            duration=arrivals[-1] - start,
            waiting_minutes=tuple(waiting),
        )
    return None
