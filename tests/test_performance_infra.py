"""Correctness tests for the shared solver performance infrastructure.

Guarantees under test:
- leg physics lookup is bit-identical to the Stage 1 rule formulas;
- cached technical-stop results equal uncached recomputation exactly;
- cache keys never merge across aircraft type / base airport / service order;
- technical-stop routes and refuel witnesses survive caching unchanged;
- assignment/load changes never hit an assignment-dependent cache entry;
- cache.clear() and shared-vs-fresh caches leave solver output identical.
"""
from __future__ import annotations

import random

import pytest

from src.rules import flight_minutes, fuel_for_leg
from src.solver.cache import SolverCache
from src.solver.data import load_problem_data
from src.solver.evaluator import evaluate_route
from src.solver.improve import _assignment_signature, _rebuild_route, improve_q1_batch_relocation
from src.solver.models import PassengerAssignment, RoutePlan, Solution, SolverConfig, aggregate_evaluations
from src.solver.physics import LegPhysics
from src.solver.technical_stops import augment_service_sequence


@pytest.fixture(scope="module")
def data():
    return load_problem_data()


@pytest.fixture(scope="module")
def cache(data):
    return SolverCache(data)


def test_leg_physics_matches_rules_exhaustively(data):
    physics = LegPhysics(data.config, data.matrix)
    for aircraft_type, aircraft in data.config.aircraft_types.items():
        for origin, row in data.matrix.items():
            for destination, distance in row.items():
                got = physics.leg(aircraft_type, origin, destination)
                assert got == (
                    distance,
                    flight_minutes(distance, aircraft.speed_kmh),
                    fuel_for_leg(distance, aircraft),
                )


def test_leg_physics_missing_pair_raises_like_matrix(data):
    partial = {"A01": {"A01": 0.0}}
    physics = LegPhysics(data.config, partial)
    with pytest.raises(KeyError):
        physics.leg("T1", "A01", "F006")


def test_cached_augmentation_equals_uncached(cache, data):
    cases = [
        ("A01", "T1", ("F006",)),
        ("A02", "T3", ("F014",)),
        ("A03", "T2", ("F043", "F044")),
        ("A01", "T2", ("F020", "F014", "F006")),
    ]
    for base, aircraft_type, order in cases:
        expected = augment_service_sequence(
            base, aircraft_type, order, matrix=data.matrix, config=data.config
        )
        before_hits = cache.stats()["augmentation_hits"]
        got = cache.augmentation_result(base, aircraft_type, order)
        assert got == expected
        # Second access must be a hit returning the identical object.
        again = cache.augmentation_result(base, aircraft_type, order)
        assert again is got
        assert cache.stats()["augmentation_hits"] == before_hits + 1


def test_refuel_witness_and_route_recovery_through_cache(cache, data):
    uncached = augment_service_sequence(
        "A01", "T1", ("F006",), matrix=data.matrix, config=data.config
    )
    cached = cache.augmentation_result("A01", "T1", ("F006",))
    assert [stop.facility_id for stop in cached.stops] == ["A01", "F006", "A01"]
    assert cached.stops[1].refuel and cached.stops[1].is_service
    assert cached.stops == uncached.stops
    assert cached.total_aircraft_time_minutes == uncached.total_aircraft_time_minutes
    assert cached.total_fuel_consumption_kg == uncached.total_fuel_consumption_kg


def test_cache_never_shares_across_type_base_or_order(cache):
    stats_before = cache.stats()
    keys = [
        ("A01", "T1", ("F014",)),
        ("A01", "T2", ("F014",)),  # different aircraft type
        ("A02", "T1", ("F014",)),  # different base airport
        ("A01", "T1", ("F020",)),  # different facility
    ]
    for base, aircraft_type, order in keys:
        cache.augmentation_result(base, aircraft_type, order)
    stats_after = cache.stats()
    assert stats_after["augmentation_misses"] == stats_before["augmentation_misses"] + len(keys)
    # Different service orders must stay separate entries too.
    cache.augmentation_result("A01", "T3", ("F020", "F014"))
    cache.augmentation_result("A01", "T3", ("F014", "F020"))
    entry = cache.augmentation.get(("A01", "T3", ("F020", "F014")))
    entry_swapped = cache.augmentation.get(("A01", "T3", ("F014", "F020")))
    assert entry is not None and entry_swapped is not None and entry is not entry_swapped


def test_cached_vs_uncached_random_differential(cache, data):
    rng = random.Random(2026)
    airports = list(data.config.airports)
    types = sorted(data.config.aircraft_types)
    facilities = list(data.config.facilities)
    saw_technical_stop = False
    for _ in range(30):
        base = rng.choice(airports)
        aircraft_type = rng.choice(types)
        size = rng.randint(1, 3)
        order = tuple(rng.sample(facilities, size))
        expected = augment_service_sequence(
            base, aircraft_type, order, matrix=data.matrix, config=data.config
        )
        got = cache.augmentation_result(base, aircraft_type, order)
        assert got == expected, (base, aircraft_type, order)
        if got.feasible and len(got.stops) > size + 2:
            saw_technical_stop = True
    assert saw_technical_stop, "sample should include at least one technical-stop route"


def test_rebuild_cache_key_respects_load_profile(data):
    cache = SolverCache(data)
    secondary_order = SolverConfig().secondary_order

    def assignments_for(count: int) -> tuple[PassengerAssignment, ...]:
        return tuple(
            PassengerAssignment(f"P{i:04d}", "LAND", "F044", 0, 0) for i in range(count)
        )

    small = assignments_for(5)
    large = assignments_for(9)
    assert _assignment_signature(small) != _assignment_signature(large)

    route_small, eval_small, _ = _rebuild_route("A03", small, data, secondary_order, cache)
    route_large, eval_large, _ = _rebuild_route("A03", large, data, secondary_order, cache)
    assert route_small is not None and route_large is not None
    # Same static skeleton is allowed, but passenger-dependent evaluation must
    # be recomputed per load profile and reflect the actual passenger count.
    assert eval_large.total_passenger_travel_time_minutes > eval_small.total_passenger_travel_time_minutes
    # A second call with the identical load profile hits the skeleton cache yet
    # still yields the exact same evaluation numbers.
    again_route, again_eval, count = _rebuild_route("A03", small, data, secondary_order, cache)
    assert count == 1
    assert again_route.aircraft_type == route_small.aircraft_type
    assert again_route.stops == route_small.stops
    assert again_eval.total_aircraft_time_minutes == eval_small.total_aircraft_time_minutes
    assert again_eval.total_passenger_travel_time_minutes == eval_small.total_passenger_travel_time_minutes


def _two_route_fixture(data) -> Solution:
    routes = []
    evaluations = []
    for aircraft_type, destination, count in (("T3", "F044", 18), ("T2", "F043", 14)):
        augmented = augment_service_sequence(
            "A03", aircraft_type, (destination,), matrix=data.matrix, config=data.config
        )
        locations = [stop.facility_id for stop in augmented.stops]
        assignments = tuple(
            PassengerAssignment(
                f"{destination}-{index}", "LAND", destination, 0, locations.index(destination)
            )
            for index in range(count)
        )
        route = RoutePlan("A03", aircraft_type, augmented.stops, assignments, (destination,))
        routes.append(route)
        evaluations.append(evaluate_route(route, matrix=data.matrix, config=data.config))
    return Solution(tuple(routes), aggregate_evaluations(evaluations, served=32))


def test_shared_cache_does_not_change_deterministic_search(data):
    baseline = _two_route_fixture(data)
    uncached = improve_q1_batch_relocation(baseline, data, max_targets_per_batch=2, max_iterations=1)
    shared = SolverCache(data)
    cached_run = improve_q1_batch_relocation(
        baseline, data, max_targets_per_batch=2, max_iterations=1, cache=shared
    )
    assert cached_run.routes == uncached.routes
    assert cached_run.metrics == uncached.metrics
    # Reusing the same cache again (cross-stage lifecycle) stays identical.
    rerun = improve_q1_batch_relocation(
        cached_run, data, max_targets_per_batch=2, max_iterations=1, cache=shared
    )
    assert rerun.metrics.total_aircraft_time_minutes == cached_run.metrics.total_aircraft_time_minutes
    # Clearing the cache must not change results either.
    shared.clear()
    cleared = improve_q1_batch_relocation(
        baseline, data, max_targets_per_batch=2, max_iterations=1, cache=shared
    )
    assert cleared.routes == uncached.routes
    assert cleared.metrics == uncached.metrics


def test_cache_stats_are_complete_and_lightweight(cache):
    stats = cache.stats()
    expected_keys = {
        "augmentation_hits",
        "augmentation_misses",
        "skeleton_hits",
        "skeleton_misses",
        "lower_bound_hits",
        "lower_bound_misses",
        "direct_time_hits",
        "direct_time_misses",
        "augmentation_entries",
        "skeleton_entries",
        "lower_bound_entries",
        "direct_time_entries",
        "leg_physics_entries",
    }
    assert expected_keys <= set(stats)
    assert stats["leg_physics_entries"] == 3 * 55 * 55
    assert all(isinstance(value, int) and value >= 0 for value in stats.values())
