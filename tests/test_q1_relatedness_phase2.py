from __future__ import annotations

from collections import Counter

import pytest

from src.solver.candidate_ranking import RawDistanceRanker, load_fuel_signatures
from src.solver.data import load_problem_data
from src.solver.models import PassengerAssignment, RoutePlan, RouteStop
from src.solver.relatedness import (
    ContextCompatibility,
    ContextRelatednessRanker,
    StaticRelatednessModel,
    StaticRelatednessRanker,
    build_static_components,
    capacity_pair_evidence,
    consensus_coassociation,
    consensus_leave_one_out_deviation,
    minimum_seat_capacity,
)


@pytest.fixture(scope="module")
def relatedness_fixture():
    data = load_problem_data()
    facilities = tuple(data.config.facilities)
    split = len(facilities) // 2
    labelings = {
        "first": {
            facility: int(index >= split) for index, facility in enumerate(facilities)
        },
        "second": {
            facility: int(index % 3 == 0) for index, facility in enumerate(facilities)
        },
    }
    consensus = consensus_coassociation(
        facilities, labelings, weights={"first": 1.0, "second": 0.8}
    )
    signatures = load_fuel_signatures(
        "data/processed/features/closed_route_reachability.csv"
    )
    components = build_static_components(
        data, consensus=consensus.matrix, fuel_signatures=signatures
    )
    model = StaticRelatednessModel.equal_weighted(
        components, ("distance", "consensus", "capacity"), name="test_static"
    )
    return data, labelings, consensus, components, model


def _route(base: str, destination: str, load: int, aircraft_type: str = "T3") -> RoutePlan:
    assignments = tuple(
        PassengerAssignment(f"P{index}", "LAND", destination, 0, 1)
        for index in range(load)
    )
    return RoutePlan(
        base,
        aircraft_type,
        (
            RouteStop(base),
            RouteStop(destination, is_service=True),
            RouteStop(base),
        ),
        assignments,
        (destination,),
    )


def test_consensus_is_deterministic_symmetric_and_has_unit_diagonal(
    relatedness_fixture,
):
    _, labelings, consensus, _, _ = relatedness_fixture
    repeated = consensus_coassociation(
        consensus.facilities,
        labelings,
        weights={"first": 1.0, "second": 0.8},
    )
    assert repeated == consensus
    for left in consensus.facilities:
        assert consensus.matrix[left][left] == 1.0
        for right in consensus.facilities:
            assert consensus.matrix[left][right] == consensus.matrix[right][left]
            assert 0.0 <= consensus.matrix[left][right] <= 1.0
    deviations = consensus_leave_one_out_deviation(consensus, labelings)
    assert set(deviations) == set(labelings)
    assert all(value >= 0 for value in deviations.values())


def test_static_components_are_symmetric_and_auditable(relatedness_fixture):
    _, _, _, components, model = relatedness_fixture
    facilities = components.facilities
    for name, matrix in components.matrices.items():
        for left in facilities:
            assert matrix[left][left] == pytest.approx(1.0), name
            for right in facilities:
                assert matrix[left][right] == pytest.approx(matrix[right][left]), name
                assert 0.0 <= matrix[left][right] <= 1.0
    values = model.facility_pair_features(facilities[0], facilities[1])
    expected = sum(
        model.weights[name] * getattr(values, name) for name in model.weights
    )
    assert values.score == pytest.approx(expected)


def test_airport_and_fuel_components_preserve_gap_order(relatedness_fixture):
    data, _, _, components, _ = relatedness_fixture
    facilities = components.facilities
    pairs = [
        (left, right)
        for index, left in enumerate(facilities)
        for right in facilities[index + 1 :]
    ]
    airport_gap = lambda pair: sum(
        abs(data.matrix[airport][pair[0]] - data.matrix[airport][pair[1]])
        for airport in data.config.airports
    ) / len(data.config.airports)
    closest = min(pairs, key=airport_gap)
    farthest = max(pairs, key=airport_gap)
    assert components.matrices["airport"][closest[0]][closest[1]] >= components.matrices[
        "airport"
    ][farthest[0]][farthest[1]]

    signatures = load_fuel_signatures(
        "data/processed/features/closed_route_reachability.csv"
    )
    signature_counts = Counter(signatures.values())
    identical_pair = next(
        (left, right)
        for left, right in pairs
        if signatures[left] == signatures[right] and signature_counts[signatures[left]] > 1
    )
    maximum_fuel_similarity = max(
        components.matrices["fuel"][left][right] for left, right in pairs
    )
    assert components.matrices["fuel"][identical_pair[0]][identical_pair[1]] == pytest.approx(
        maximum_fuel_similarity
    )


def test_capacity_evidence_handles_aircraft_breakpoints():
    capacities = (12, 16, 19)
    assert minimum_seat_capacity(0, capacities) == 0
    assert minimum_seat_capacity(11, capacities) == 12
    assert minimum_seat_capacity(13, capacities) == 16
    assert minimum_seat_capacity(17, capacities) == 19
    assert minimum_seat_capacity(20, capacities) == 24
    evidence = capacity_pair_evidence(11, 8, capacities)
    assert evidence.separate_seats == 24
    assert evidence.combined_seats == 19
    assert evidence.saved_seats == 5
    assert evidence.combined_utilization == 1.0


def test_context_responds_to_slack_and_respects_airport_semantics(
    relatedness_fixture,
):
    data, _, _, _, model = relatedness_fixture
    context = ContextCompatibility(model)
    target = _route("A01", "F001", 5)
    small = context.facility_to_route("F002", 5, "LAND", target, data)
    filling = context.facility_to_route("F002", 14, "LAND", target, data)
    fixed_wrong = context.facility_to_route("F002", 5, "A02", target, data)
    fixed_right = context.facility_to_route("F002", 5, "A01", target, data)
    assert small.allowed and filling.allowed
    assert filling.capacity_fill > small.capacity_fill
    assert filling.score > small.score
    assert not fixed_wrong.allowed and fixed_wrong.score == 0.0
    assert fixed_right.allowed


def test_static_and_context_rankers_are_deterministic_and_legacy_is_unchanged(
    relatedness_fixture,
):
    data, _, _, _, model = relatedness_fixture
    left = _route("A01", "F001", 5)
    right = _route("A01", "F002", 8)
    raw = RawDistanceRanker()
    raw_key = raw.pair_key(left, right, 0, 1, data)
    static = StaticRelatednessRanker(model)
    context = ContextRelatednessRanker(static, ContextCompatibility(model))
    assert static.pair_key(left, right, 0, 1, data) == static.pair_key(
        left, right, 0, 1, data
    )
    assert context.pair_key(left, right, 0, 1, data) == context.pair_key(
        left, right, 0, 1, data
    )
    assert raw.pair_key(left, right, 0, 1, data) == raw_key
    features = context.features(left, right, data)
    assert features.static_relatedness_score is not None
    assert features.context_relatedness_score is not None
    assert features.combined_candidate_score is not None
