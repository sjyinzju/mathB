from __future__ import annotations

import pytest

from src.solver.candidate_ranking import (
    ClusterCandidateRanker,
    RelatednessModel,
)
from src.solver.clustering import (
    adjusted_rand_index,
    average_linkage,
    pam_k_medoids,
    validate_dissimilarity,
    with_stability,
)
from src.solver.data import load_problem_data
from src.solver.evaluator import evaluate_route
from src.solver.improve import improve_q1_savings
from src.solver.models import PassengerAssignment, RoutePlan, Solution, aggregate_evaluations
from src.solver.technical_stops import augment_service_sequence


def _separated_matrix():
    facilities = ("F1", "F2", "F3", "F4")
    values = (
        (0, 1, 10, 11),
        (1, 0, 9, 10),
        (10, 9, 0, 1),
        (11, 10, 1, 0),
    )
    return facilities, {
        left: {right: float(values[i][j]) for j, right in enumerate(facilities)}
        for i, left in enumerate(facilities)
    }


def test_pam_is_deterministic_and_uses_real_facility_medoids():
    facilities, matrix = _separated_matrix()
    first = pam_k_medoids(facilities, matrix, 2)
    second = pam_k_medoids(facilities, matrix, 2)
    assert first == second
    assert first.labels == (0, 0, 1, 1)
    assert set(first.medoids).issubset(facilities)
    assert first.cluster_sizes == (2, 2)
    assert first.silhouette > 0.8


def test_average_linkage_accepts_precomputed_non_metric_distance_without_closure():
    facilities = ("F1", "F2", "F3", "F4")
    matrix = {
        "F1": {"F1": 0.0, "F2": 1.0, "F3": 10.0, "F4": 11.0},
        "F2": {"F1": 1.0, "F2": 0.0, "F3": 1.0, "F4": 10.0},
        "F3": {"F1": 10.0, "F2": 1.0, "F3": 0.0, "F4": 1.0},
        "F4": {"F1": 11.0, "F2": 10.0, "F3": 1.0, "F4": 0.0},
    }
    validate_dissimilarity(facilities, matrix)
    result = average_linkage(facilities, matrix, 2)
    assert result.k == 2
    assert matrix["F1"]["F3"] == 10.0
    assert result.within_dissimilarity >= 0


def test_stability_is_reproducible_and_ari_is_label_permutation_invariant():
    facilities, matrix = _separated_matrix()
    result = pam_k_medoids(facilities, matrix, 2)
    first = with_stability(result, matrix, repeats=8, seed=7)
    second = with_stability(result, matrix, repeats=8, seed=7)
    assert first.stability_median_ari == second.stability_median_ari == 1.0
    assert adjusted_rand_index((0, 0, 1, 1), (1, 1, 0, 0)) == 1.0


def test_invalid_k_and_asymmetric_matrix_are_rejected():
    facilities, matrix = _separated_matrix()
    with pytest.raises(ValueError):
        pam_k_medoids(facilities, matrix, 1)
    broken = {left: dict(row) for left, row in matrix.items()}
    broken["F1"]["F2"] = 2.0
    with pytest.raises(ValueError):
        validate_dissimilarity(facilities, broken)


def _single_person_solution(data, destinations):
    routes = []
    evaluations = []
    for index, destination in enumerate(destinations):
        augmented = augment_service_sequence(
            "A01", "T3", (destination,), matrix=data.matrix, config=data.config
        )
        assert augmented.feasible
        locations = tuple(stop.facility_id for stop in augmented.stops)
        route = RoutePlan(
            "A01",
            "T3",
            augmented.stops,
            (
                PassengerAssignment(
                    f"TEST{index}", "LAND", destination, 0, locations.index(destination)
                ),
            ),
            (destination,),
        )
        routes.append(route)
        evaluations.append(evaluate_route(route, matrix=data.matrix, config=data.config))
    return Solution(tuple(routes), aggregate_evaluations(evaluations, served=len(routes)))


def test_cluster_ranking_is_soft_and_full_candidate_audit_is_order_invariant(config):
    data = load_problem_data(config=config)
    baseline = _single_person_solution(data, ("F014", "F020", "F025"))
    facilities = tuple(data.config.facilities)
    fitted = pam_k_medoids(facilities, data.matrix, 4)
    ranker = ClusterCandidateRanker(
        RelatednessModel(fitted, data.matrix, tuple(data.config.airports))
    )
    left, right = baseline.routes[0], baseline.routes[2]
    assert ranker.pair_key(left, right, 0, 2, data)

    raw = improve_q1_savings(
        baseline, data, candidate_mode="global", pair_budget=None
    )
    clustered = improve_q1_savings(
        baseline,
        data,
        candidate_ranker=ranker,
        candidate_mode="global",
        pair_budget=None,
    )
    assert clustered.metrics.comparison_key() == raw.metrics.comparison_key()


def test_fixed_pair_budget_and_candidate_event_schema(config):
    data = load_problem_data(config=config)
    baseline = _single_person_solution(data, ("F014", "F020", "F025"))
    events: list[dict[str, object]] = []
    result = improve_q1_savings(
        baseline,
        data,
        candidate_mode="global",
        pair_budget=1,
        max_iterations=1,
        candidate_events=events,
    )
    stats = result.diagnostics["generalized_savings"]
    assert stats["evaluated_pairs"] == 1
    assert sum(bool(event["selected"]) for event in events) == 1
    assert sum(int(event["route_evaluations"]) for event in events) == stats["evaluated_routes"]
    assert sum(int(event["technical_stop_searches"]) for event in events) == stats[
        "technical_stop_searches"
    ]
    assert sum(bool(event["accepted"]) for event in events) == stats["accepted_merges"]
    assert any(event["outcome"] == "budget_not_selected" for event in events)
    selected = next(event for event in events if event["selected"])
    required = {
        "iteration",
        "candidate_rank",
        "left_signature",
        "right_signature",
        "minimum_distance_km",
        "airport_profile_gap_km",
        "outcome",
        "route_evaluations",
        "technical_stop_searches",
        "accepted",
    }
    assert required.issubset(selected)


def test_relatedness_result_rejects_missing_facility():
    facilities, matrix = _separated_matrix()
    fitted = pam_k_medoids(facilities, matrix, 2)
    model = RelatednessModel(fitted, matrix, ("F1",))
    with pytest.raises(KeyError):
        model.relatedness("F1", "UNKNOWN")
