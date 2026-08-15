from __future__ import annotations

from collections import Counter
from pathlib import Path

from src.config import ROOT
from src.solver import (
    SolverCache,
    export_q1_solution,
    load_problem_data,
    load_q2_solution,
)
from src.validation import validate_solution
from src.solver.q2 import (
    assignment_interval,
    build_q2_variant,
    candidate_pool_hash,
    candidate_service_sequences,
    q2_direction,
)


def test_q2_data_counts_and_directions() -> None:
    data = load_problem_data()
    assert data.q2_passenger_count == 4000
    assert len(data.q2_pools) == 264
    groups = Counter(
        q2_direction(origin, destination, data.config.airports)
        for origin, destination in data.q2_pools
    )
    passengers = Counter()
    for key, pool in data.q2_pools.items():
        passengers[q2_direction(key[0], key[1], data.config.airports)] += pool.quantity
    assert groups == {"outbound": 104, "inbound": 104, "shuttle": 56}
    assert passengers == {"outbound": 1600, "inbound": 1600, "shuttle": 800}


def test_q2_land_fixed_airport_and_shuttle_intervals() -> None:
    data = load_problem_data()
    cache = SolverCache(data)
    variant = build_q2_variant(data, "A01", "T3", ("F021", "F022"), cache=cache)
    assert variant is not None
    outbound = assignment_interval(variant, "LAND", "F021", data.config.airports)
    shuttle = assignment_interval(variant, "F021", "F022", data.config.airports)
    inbound = assignment_interval(variant, "F022", "LAND", data.config.airports)
    fixed_ok = assignment_interval(variant, "A01", "F021", data.config.airports)
    fixed_wrong = assignment_interval(variant, "A02", "F021", data.config.airports)
    locations = tuple(stop.facility_id for stop in variant.route.stops)
    f021 = locations.index("F021")
    f022 = locations.index("F022", f021 + 1)
    assert outbound is not None and outbound[:2] == (0, f021)
    assert shuttle is not None and shuttle[:2] == (f021, f022)
    assert inbound is not None and inbound[0] == f022
    assert fixed_ok is not None
    assert fixed_wrong is None


def test_q2_variant_uses_shared_cache_without_semantic_change() -> None:
    data = load_problem_data()
    cache = SolverCache(data)
    first = build_q2_variant(data, "A01", "T3", ("F021", "F022"), cache=cache)
    second = build_q2_variant(data, "A01", "T3", ("F021", "F022"), cache=cache)
    uncached = build_q2_variant(data, "A01", "T3", ("F021", "F022"))
    assert first == second == uncached
    stats = cache.stats()
    assert stats["augmentation_misses"] == 1
    assert stats["augmentation_hits"] == 1


def test_q2_candidate_generation_and_hash_are_deterministic() -> None:
    data = load_problem_data()
    sequences_a = candidate_service_sequences(data)
    sequences_b = candidate_service_sequences(data)
    assert sequences_a == sequences_b
    assert all(1 <= len(sequence) <= 2 for sequence in sequences_a)
    cache = SolverCache(data)
    variants = tuple(
        variant
        for sequence in sequences_a[:4]
        for variant in (
            build_q2_variant(data, "A01", "T3", sequence, cache=cache),
        )
        if variant is not None
    )
    assert candidate_pool_hash(variants) == candidate_pool_hash(tuple(variants))


def test_checked_in_q2_baseline_round_trips_and_validates(tmp_path: Path) -> None:
    data = load_problem_data()
    baseline = ROOT / "outputs" / "q2" / "baseline-19736"
    solution = load_q2_solution(
        baseline / "q2-routes.csv",
        baseline / "q2-assignments.csv",
        data,
    )
    assert solution.metrics.to_dict() == {
        "total_aircraft_time_minutes": 19736,
        "total_passenger_travel_time_minutes": 270734,
        "total_flights": 107,
        "total_fuel_consumption_kg": 152910.4,
        "seat_utilization": 0.8182982554006456,
        "served_passengers": 4000,
    }
    exported_routes = tmp_path / "q2-routes.csv"
    exported_assignments = tmp_path / "q2-assignments.csv"
    export_q1_solution(solution, exported_routes, exported_assignments)
    validation = validate_solution(
        "q2",
        exported_routes,
        exported_assignments,
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    assert validation.valid
    assert not validation.issues
    assert (baseline / "q2-routes.csv").read_bytes() == exported_routes.read_bytes()
    assert (baseline / "q2-assignments.csv").read_bytes() == exported_assignments.read_bytes()


def test_q2_best_is_an_atomic_single_run_copy() -> None:
    baseline = ROOT / "outputs" / "q2" / "baseline-19736"
    best = ROOT / "outputs" / "q2" / "best"
    baseline_files = {path.name for path in baseline.iterdir() if path.is_file()}
    best_files = {path.name for path in best.iterdir() if path.is_file()}
    assert best_files == baseline_files
    assert not any(name.startswith("q2-pair-") for name in best_files)
    for name in baseline_files:
        assert (baseline / name).read_bytes() == (best / name).read_bytes()
