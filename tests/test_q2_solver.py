from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from src.config import ROOT
from src.solver import (
    Q2LnsConfig,
    SolverCache,
    build_q2_local_data,
    exact_q2_local_repair,
    export_q1_solution,
    geometry_local_sequences,
    load_problem_data,
    load_q2_solution,
    select_q2_neighborhood,
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
    baseline_metrics = json.loads((baseline / "metrics.json").read_text(encoding="utf-8"))
    assert baseline_metrics["validator_metrics"]["total_aircraft_time_minutes"] == 19736
    run_config = json.loads((best / "run_config.json").read_text(encoding="utf-8"))
    source_run = ROOT / "outputs" / "q2" / "runs" / run_config["run_id"]
    assert source_run.is_dir()
    best_files = {path.name for path in best.iterdir() if path.is_file()}
    source_files = {path.name for path in source_run.iterdir() if path.is_file()}
    assert best_files == source_files
    assert not any(name.startswith("q2-pair-") for name in best_files)
    for name in source_files:
        assert (source_run / name).read_bytes() == (best / name).read_bytes()


def test_q2_local_destroy_data_preserves_exact_people_and_sequences() -> None:
    data = load_problem_data()
    baseline = ROOT / "outputs" / "q2" / "baseline-19736"
    solution = load_q2_solution(
        baseline / "q2-routes.csv",
        baseline / "q2-assignments.csv",
        data,
    )
    routes = solution.routes[:3]
    local = build_q2_local_data(data, routes)
    expected_people = {
        assignment.person_id for route in routes for assignment in route.assignments
    }
    actual_people = {
        person_id for pool in local.q2_pools.values() for person_id in pool.person_ids
    }
    assert actual_people == expected_people
    sequences_a = geometry_local_sequences(
        local, routes, max_sequence_length=2, budget=24
    )
    sequences_b = geometry_local_sequences(
        local, routes, max_sequence_length=2, budget=24
    )
    assert sequences_a == sequences_b
    assert all(tuple(route.service_facilities) in sequences_a for route in routes)
    assert all(1 <= len(sequence) <= data.config.max_sea_landings for sequence in sequences_a)


def test_q2_destroy_neighborhood_is_seed_deterministic() -> None:
    data = load_problem_data()
    baseline = ROOT / "outputs" / "q2" / "baseline-19736"
    solution = load_q2_solution(
        baseline / "q2-routes.csv",
        baseline / "q2-assignments.csv",
        data,
    )
    config = Q2LnsConfig(iterations=1, seed=7)
    first = select_q2_neighborhood(
        solution,
        data,
        operator="high_cost_route",
        iteration=0,
        config=config,
    )
    second = select_q2_neighborhood(
        solution,
        data,
        operator="high_cost_route",
        iteration=0,
        config=config,
    )
    assert first == second
    assert len(first) == config.neighborhood_size
    assert len(set(first)) == config.neighborhood_size


def test_q2_exact_local_repair_only_returns_primary_improvement() -> None:
    data = load_problem_data()
    baseline = ROOT / "outputs" / "q2" / "baseline-19736"
    solution = load_q2_solution(
        baseline / "q2-routes.csv",
        baseline / "q2-assignments.csv",
        data,
    )
    config = Q2LnsConfig(
        iterations=1,
        neighborhood_size=3,
        max_sequence_length=2,
        candidate_sequence_budget=8,
        local_primary_seconds=5.0,
        local_secondary_seconds=0.0,
        operators=("land_heavy_route",),
    )
    repair = exact_q2_local_repair(
        solution,
        data,
        (18, 46, 53),
        cache=SolverCache(data),
        config=config,
    )
    assert repair.solution is not None
    assert (
        repair.solution.metrics.total_aircraft_time_minutes
        < solution.metrics.total_aircraft_time_minutes
    )
    assert repair.diagnostics["after_routes"] <= repair.diagnostics["before_routes"]
