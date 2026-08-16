from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from src.solver import load_problem_data
from src.solver.q3 import (
    Q3Flight,
    Q3Person,
    _assignment_for_person,
    load_q3_people,
    load_q3_schedule,
    load_q3_variants,
    optimize_fixed_flight_assignments,
    project_mandatory_only,
    schedule_metrics,
)
from src.solver.q3_closure_p2 import (
    build_mandatory_schedule_flexible_regret,
    generalized_multiflight_ruin_recreate,
    stage1_key,
    stage2_key,
)


ROOT = Path(__file__).resolve().parents[1]


def _loaded_v8():
    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    variants = load_q3_variants(
        ROOT / "outputs/q2/pair_n3_h10.pkl", people.values(), data.config
    )
    flights = load_q3_schedule(
        ROOT / "outputs/q3/p0_p1_best/q3-routes.csv",
        ROOT / "outputs/q3/p0_p1_best/q3-assignments.csv",
        people,
        variants,
        data.config,
    )
    return data, people, variants, flights


def test_projection_is_deep_and_does_not_mutate_input() -> None:
    _data, people, _variants, flights = _loaded_v8()
    before = deepcopy(flights)
    projected = project_mandatory_only(flights, people)
    assert schedule_metrics(flights, people) == schedule_metrics(before, people)
    assert all(people[pid].mandatory for flight in projected for pid in flight.person_ids)
    assert [id(flight) for flight in projected] != [id(flight) for flight in flights]


def test_stage1_optimizer_cannot_reinsert_temporary() -> None:
    data, people, _variants, flights = _loaded_v8()
    mandatory = {pid: p for pid, p in people.items() if p.mandatory}
    projected = project_mandatory_only(flights, people)
    repaired, unserved, _stats = optimize_fixed_flight_assignments(
        projected, mandatory, data.config, time_limit_seconds=20.0
    )
    assert not unserved
    assert all(pid in mandatory for flight in repaired for pid in flight.person_ids)


def test_lexicographic_keys_prioritize_stage_semantics() -> None:
    _data, people, _variants, flights = _loaded_v8()
    projected = project_mandatory_only(flights, people)
    assert stage1_key(projected, people)[0] == schedule_metrics(projected, people)["total_aircraft_time_minutes"]
    assert stage2_key(flights, people)[0] == -schedule_metrics(flights, people)["served_optional"]


def test_hard_skeleton_then_zero_cost_flexible_insertion() -> None:
    data, people, variants, flights = _loaded_v8()
    variant = flights[0].variant
    od = next(
        person.od
        for person in people.values()
        if _assignment_for_person(person, variant, data.config) is not None
    )
    synthetic = {
        "H": Q3Person("H", od[0], od[1], 0, 7 * 1440 - 1, "emergency", 1),
        "F": Q3Person("F", od[0], od[1], 0, 7 * 1440 - 1, "shift", 3),
    }
    schedule, stats = build_mandatory_schedule_flexible_regret(
        synthetic,
        {od: variants[od]},
        data,
        seed=7,
        hard_day_threshold=0,
        hard_window_minutes=0,
        scarce_route_threshold=0,
    )
    assert stats["feasible"]
    assert stats["hard_count"] == 1
    assert stats["zero_cost_inserted"] == 1
    assert len(schedule) == 1


def _synthetic_multiflight(person_count: int, old_flight_count: int):
    data, people, variants, real_flights = _loaded_v8()
    variant = next(
        flight.variant
        for flight in real_flights
        if flight.variant.capacity >= 8 and flight.duration <= 240
    )
    compatible_person = next(
        person
        for person in people.values()
        if _assignment_for_person(person, variant, data.config) is not None
    )
    od = compatible_person.od
    capacity = variant.capacity
    synthetic = {
        f"S{index:03d}": Q3Person(
            f"S{index:03d}", od[0], od[1], 0, 1200, "shift", 3
        )
        for index in range(person_count)
    }
    aircraft = [
        aircraft_id
        for aircraft_id in data.config.fleet_ids
        if aircraft_id.startswith(f"{variant.base_airport}-{variant.aircraft_type}-")
    ]
    assignment = _assignment_for_person(next(iter(synthetic.values())), variant, data.config)
    assert assignment is not None
    flights = []
    ids = list(synthetic)
    for index in range(old_flight_count):
        chunk = ids[index::old_flight_count]
        flights.append(
            Q3Flight(
                variant=variant,
                aircraft_id=aircraft[index % len(aircraft)],
                start=360 + (index // len(aircraft)) * (variant.duration + 30),
                person_ids=chunk,
                assignment_intervals={pid: assignment for pid in chunk},
            )
        )
    return data, synthetic, {od: (variant,)}, flights, capacity


def test_generalized_static_pool_supports_three_to_two() -> None:
    _data, _people, _variants, _flights, capacity = _synthetic_multiflight(1, 3)
    # Ensure the demand requires two replacement flights but no more.
    count = capacity + 1
    data, people, variants, flights, _capacity = _synthetic_multiflight(count, 3)
    improved, trace = generalized_multiflight_ruin_recreate(
        flights,
        people,
        variants,
        data,
        stage=1,
        group_min=3,
        group_max=3,
        maximum_trials=3,
        maximum_neighbors=3,
        route_limit=10,
        assignment_time_limit_seconds=20.0,
        seed=11,
    )
    assert trace["accepted_histogram"].get("3->2", 0) >= 1
    assert len(improved) == 2


def test_rejected_km_move_does_not_mutate_input() -> None:
    data, people, variants, flights, capacity = _synthetic_multiflight(2 * 8, 2)
    before = deepcopy(flights)
    _candidate, trace = generalized_multiflight_ruin_recreate(
        flights,
        people,
        variants,
        data,
        stage=1,
        group_min=2,
        group_max=2,
        maximum_trials=1,
        route_limit=1,
        assignment_time_limit_seconds=10.0,
    )
    assert schedule_metrics(flights, people) == schedule_metrics(before, people)
    assert trace["trials"] <= 1
