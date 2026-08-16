from __future__ import annotations

import random
from pathlib import Path

from src.solver import load_problem_data
from src.solver.q3 import (
    Q3Person,
    _assignment_for_person,
    _seed_key,
    build_flexibility_profiles,
    load_q3_people,
    load_q3_schedule,
    load_q3_variants,
    project_mandatory_only,
    schedule_metrics,
)
from src.solver.q3_timing import schedule_route_timing


ROOT = Path(__file__).resolve().parents[1]


def _loaded_best():
    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    variants = load_q3_variants(
        ROOT / "outputs/q2/pair_n3_h10.pkl", people.values(), data.config
    )
    flights = load_q3_schedule(
        ROOT / "outputs/q3/best/q3-routes.csv",
        ROOT / "outputs/q3/best/q3-assignments.csv",
        people,
        variants,
        data.config,
    )
    return data, people, variants, flights


def test_project_mandatory_only_removes_optional_only() -> None:
    _data, people, _variants, flights = _loaded_best()
    projected = project_mandatory_only(flights, people)
    assert all(
        people[person_id].mandatory
        for flight in projected
        for person_id in flight.person_ids
    )
    assert sum(len(flight.person_ids) for flight in projected) == 3840
    assert [
        (flight.variant.key, flight.aircraft_id, flight.start)
        for flight in projected
    ] == [
        (flight.variant.key, flight.aircraft_id, flight.start) for flight in flights
    ]


def test_projection_cannot_increase_aircraft_time() -> None:
    _data, people, _variants, flights = _loaded_best()
    projected = project_mandatory_only(flights, people)
    assert schedule_metrics(projected, people)["total_aircraft_time_minutes"] == schedule_metrics(flights, people)["total_aircraft_time_minutes"]


def test_extended_seed_modes_are_deterministic() -> None:
    data, people, variants, _flights = _loaded_best()
    mandatory = [person for person in people.values() if person.mandatory]
    profiles = build_flexibility_profiles(mandatory[:12], variants, data.config)
    for mode in (
        "day_scarcity",
        "route_scarcity",
        "criticality",
        "od_density",
        "regret_proxy",
        "flexible_regret",
    ):
        left_rng = random.Random(19)
        right_rng = random.Random(19)
        left = [
            _seed_key(person, mode, left_rng, profiles[person.person_id])
            for person in mandatory[:12]
        ]
        right = [
            _seed_key(person, mode, right_rng, profiles[person.person_id])
            for person in mandatory[:12]
        ]
        assert left == right


def test_time_aware_waiting_makes_fixed_shift_conflict_feasible() -> None:
    data, people, variants, flights = _loaded_best()
    variant = next(
        flight.variant
        for flight in flights
        if len(flight.variant.source.route.stops) >= 4
    )
    stops = variant.source.route.stops
    first = stops[1].facility_id
    later = stops[-2].facility_id
    day = 0
    start = 360
    first_arrival = start + variant.source.arrivals[1]
    first_departure = start + variant.source.departures[1]
    synthetic = {
        "EARLY": Q3Person(
            "EARLY",
            variant.base_airport,
            first,
            start,
            first_arrival,
            "emergency",
            1,
        ),
        "LATE": Q3Person(
            "LATE",
            first,
            later,
            first_departure + 30,
            start + 1200,
            "production",
            2,
        ),
    }
    assignments = {
        person_id: _assignment_for_person(person, variant, data.config)
        for person_id, person in synthetic.items()
    }
    assert all(value is not None for value in assignments.values())
    timing = schedule_route_timing(
        variant,
        assignments,  # type: ignore[arg-type]
        synthetic,
        day,
        data.config,
    )
    assert timing is not None
    assert timing.departures[0] == start
    assert sum(timing.waiting_minutes) >= 30
    assert timing.arrivals[1] <= synthetic["EARLY"].latest
    late_pickup = assignments["LATE"][0]  # type: ignore[index]
    assert timing.departures[late_pickup] >= synthetic["LATE"].earliest
