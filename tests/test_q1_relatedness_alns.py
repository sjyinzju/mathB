from __future__ import annotations

import pytest

from src.config import ROOT
from src.solver import load_problem_data, load_q1_solution
from src.solver.relatedness import (
    FrozenConsensus,
    RepairCandidateSpec,
    rank_context_repair_candidates,
    rank_related_routes,
    raw_route_distance,
    route_service_nodes,
)


@pytest.fixture(scope="module")
def data():
    return load_problem_data()


@pytest.fixture(scope="module")
def routes(data):
    solution = load_q1_solution(
        ROOT / "outputs" / "q1" / "best" / "q1-routes.csv",
        ROOT / "outputs" / "q1" / "best" / "q1-assignments.csv",
        data,
    )
    return solution.routes


@pytest.fixture(scope="module")
def consensus(data):
    return FrozenConsensus.from_pair_csv(
        ROOT / "data" / "q1-relatedness-consensus.csv",
        data.config.facilities,
    )


def test_raw_distance_matches_phase2_definition(data, routes):
    left, right = routes[:2]
    expected = min(
        data.matrix[a][b]
        for a in route_service_nodes(left)
        for b in route_service_nodes(right)
    )
    assert raw_route_distance(left, right, data) == expected


def test_frozen_consensus_is_complete_symmetric_and_bounded(data, consensus):
    assert consensus.facilities == tuple(data.config.facilities)
    for left in consensus.facilities:
        assert consensus.matrix[left][left] == 1.0
        for right in consensus.facilities:
            assert consensus.matrix[left][right] == consensus.matrix[right][left]
            assert 0.0 <= consensus.matrix[left][right] <= 1.0


def test_distance_ranking_uses_no_airport_or_origin_penalty(data, routes):
    anchor = routes[0]
    candidates = list(range(1, min(20, len(routes))))
    ranked = rank_related_routes(
        anchor,
        candidates,
        routes,
        data,
        mode="distance",
    )
    assert ranked == sorted(
        candidates,
        key=lambda index: (raw_route_distance(anchor, routes[index], data), index),
    )


def test_consensus_is_soft_and_deterministic(data, routes, consensus):
    anchor = routes[0]
    candidates = list(range(1, min(20, len(routes))))
    first = rank_related_routes(
        anchor,
        candidates,
        routes,
        data,
        mode="distance_consensus",
        consensus=consensus,
    )
    second = rank_related_routes(
        anchor,
        candidates,
        routes,
        data,
        mode="distance_consensus",
        consensus=consensus,
    )
    assert first == second
    assert set(first) == set(candidates)


def test_context_v2_rank_is_explainable_and_component_ablated(data, routes):
    destroyed = routes[:3]
    groups = {}
    for route in destroyed:
        for assignment in route.assignments:
            groups.setdefault(
                (assignment.origin_id, assignment.destination_id), []
            ).append(assignment)
    destinations = tuple(sorted({destination for _, destination in groups}))
    candidates = [
        RepairCandidateSpec(base, aircraft, order)
        for base in data.config.airports
        for aircraft in sorted(data.config.aircraft_types)
        for order in ((destinations[0],), destinations[:2])
    ]
    ranked, features = rank_context_repair_candidates(
        candidates,
        groups,
        destroyed,
        data,
        components=("geometry", "capacity", "ejection", "airport", "route_state"),
    )
    geometry_only, _ = rank_context_repair_candidates(
        candidates,
        groups,
        destroyed,
        data,
        components=("geometry",),
    )
    assert set(ranked) == set(candidates)
    assert set(geometry_only) == set(candidates)
    assert all(0.0 <= item.rank_score <= 1.0 for item in features.values())
    assert all(item.geometry_km > 0.0 for item in features.values())
