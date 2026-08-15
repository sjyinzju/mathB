from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from src.config import ROOT
from src.solver import (
    Q2LnsConfig,
    Q2EliteEntry,
    Q2ElitePool,
    PromisingLocalMasterQueue,
    SolverCache,
    adaptive_q2_destroy_size,
    absorption_potential_ranking,
    audit_q2_solution,
    build_q2_local_data,
    build_q2_directed_flow_graph,
    classify_q2_candidate_event,
    exact_q2_local_repair,
    export_q1_solution,
    geometry_local_sequences,
    grouped_q2_splits,
    flow_aware_local_sequences,
    load_problem_data,
    load_q2_solution,
    q2_solution_diversity,
    q2_basin_fingerprint,
    q2_local_branching_feasibility,
    rank_q2_local_sequences,
    select_q2_neighborhood,
    select_absorption_neighborhood,
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


def test_q2_adaptive_destroy_size_is_explainable_and_deterministic() -> None:
    config = Q2LnsConfig(
        destroy_size_policy="adaptive",
        adaptive_destroy_sizes=(2, 3, 4),
        medium_stagnation=2,
        large_stagnation=4,
        large_neighborhood_frequency=2,
    )
    assert adaptive_q2_destroy_size(
        config,
        iteration=0,
        stagnation=0,
        recent_success_rate=1.0,
        recent_mean_runtime=0.0,
    ) == 2
    assert adaptive_q2_destroy_size(
        config,
        iteration=1,
        stagnation=2,
        recent_success_rate=0.5,
        recent_mean_runtime=0.0,
    ) == 3
    assert adaptive_q2_destroy_size(
        config,
        iteration=4,
        stagnation=4,
        recent_success_rate=0.0,
        recent_mean_runtime=0.0,
    ) == 4


def test_q2_ejection_chain_neighborhood_is_deterministic() -> None:
    data = load_problem_data()
    best = ROOT / "outputs" / "q2" / "best"
    solution = load_q2_solution(
        best / "q2-routes.csv", best / "q2-assignments.csv", data
    )
    config = Q2LnsConfig(
        operators=("ejection_chain",),
        neighborhood_size=4,
        seed=3,
    )
    first = select_q2_neighborhood(
        solution,
        data,
        operator="ejection_chain",
        iteration=5,
        config=config,
        neighborhood_size=4,
    )
    second = select_q2_neighborhood(
        solution,
        data,
        operator="ejection_chain",
        iteration=5,
        config=config,
        neighborhood_size=4,
    )
    assert first == second
    assert len(first) == len(set(first)) == 4


def test_q2_context_ranking_logs_selected_and_censored_candidates() -> None:
    data = load_problem_data()
    best = ROOT / "outputs" / "q2" / "best"
    solution = load_q2_solution(
        best / "q2-routes.csv", best / "q2-assignments.csv", data
    )
    routes = solution.routes[48:52]
    local = build_q2_local_data(data, routes)
    sequences, features, rows = rank_q2_local_sequences(
        local,
        routes,
        max_sequence_length=4,
        budget=12,
        policy="context",
        flow_graph=build_q2_directed_flow_graph(data),
        prioritize_four_stop=True,
    )
    assert sequences
    assert features
    assert any(row["top_k_selected"] for row in rows)
    assert any(row["label_censored"] for row in rows)
    assert all(
        row["evaluation_state"] == "not_evaluated"
        for row in rows
        if row["label_censored"]
    )
    assert any(len(sequence) == 4 for sequence in sequences)


def test_q2_portfolio_budget_is_deterministic_and_keeps_exploration_distinct() -> None:
    data = load_problem_data()
    best = ROOT / "outputs" / "q2" / "best"
    solution = load_q2_solution(
        best / "q2-routes.csv", best / "q2-assignments.csv", data
    )
    routes = solution.routes[48:52]
    local = build_q2_local_data(data, routes)
    kwargs = {
        "max_sequence_length": 5,
        "budget": 24,
        "policy": "portfolio",
        "flow_graph": build_q2_directed_flow_graph(data),
        "portfolio_geometry_slots": 10,
        "portfolio_context_slots": 6,
        "exploration_slots": 2,
        "selection_seed": 91,
    }
    first, _, rows_a = rank_q2_local_sequences(local, routes, **kwargs)
    second, _, rows_b = rank_q2_local_sequences(local, routes, **kwargs)
    assert first == second
    assert rows_a == rows_b
    assert len(first) <= 24
    selected_sources = {
        row["portfolio_source"] for row in rows_a if row["top_k_selected"]
    }
    assert "geometry" in selected_sources
    assert "exploration" in selected_sources
    assert any(
        row["rank_score_context"] > 0
        for row in rows_a
        if row["top_k_selected"] and row["portfolio_source"] != "incumbent"
    )


def test_q2_structured_neighborhoods_are_deterministic() -> None:
    data = load_problem_data()
    best = ROOT / "outputs" / "q2" / "best"
    solution = load_q2_solution(
        best / "q2-routes.csv", best / "q2-assignments.csv", data
    )
    for operator in ("flight_elimination", "fix_and_optimize", "cross_exchange"):
        config = Q2LnsConfig(operators=(operator,), neighborhood_size=5, seed=4)
        first = select_q2_neighborhood(
            solution, data, operator=operator, iteration=3, config=config
        )
        second = select_q2_neighborhood(
            solution, data, operator=operator, iteration=3, config=config
        )
        assert first == second
        assert len(first) == len(set(first)) == 5


def test_q2_elite_pool_keeps_quality_and_diversity() -> None:
    data = load_problem_data()
    best = ROOT / "outputs" / "q2" / "best"
    partner_dir = ROOT / "outputs" / "q2" / "runs" / "20260815-q2-final-elite-diverse"
    left = load_q2_solution(best / "q2-routes.csv", best / "q2-assignments.csv", data)
    partner = load_q2_solution(
        partner_dir / "q2-routes.csv", partner_dir / "q2-assignments.csv", data
    )
    pool = Q2ElitePool(max_size=3, min_diversity=0.0, quality_slack_minutes=600)
    assert pool.promote(Q2EliteEntry("best", left, str(best)))
    assert pool.promote(Q2EliteEntry("partner", partner, str(partner_dir)))
    assert not pool.promote(Q2EliteEntry("best", left, str(best)))
    assert pool.select_partner(left, diversity_aware=True).solution_id == "partner"


def test_q2_local_branching_gate_does_not_claim_unsupported_semantics() -> None:
    assessment = q2_local_branching_feasibility()
    assert assessment["decision"] == "REJECT"
    assert not assessment["feasible_without_master_refactor"]


def test_q2_candidate_labels_do_not_treat_censored_as_negative() -> None:
    assert classify_q2_candidate_event({"evaluation_state": "not_evaluated"}) == "CENSORED"
    assert classify_q2_candidate_event(
        {"evaluation_state": "exact_evaluated", "exact_variant_generated": False}
    ) == "INVALID"
    assert classify_q2_candidate_event(
        {
            "evaluation_state": "exact_evaluated",
            "exact_variant_generated": True,
            "milp_selected": False,
        }
    ) == "TRUE_NEGATIVE"
    assert classify_q2_candidate_event(
        {
            "evaluation_state": "exact_evaluated",
            "exact_variant_generated": True,
            "milp_selected": True,
            "repair_accepted": True,
            "primary_gain": 1,
        }
    ) == "POSITIVE"


def test_q2_learning_splits_are_run_grouped_and_deterministic() -> None:
    rows = [
        {"run_id": "run-a", "seed": 0},
        {"run_id": "run-b", "seed": 1},
        {"run_id": "run-c", "seed": 2},
        {"run_id": "run-a", "seed": 0},
    ]
    first = grouped_q2_splits(rows)
    second = grouped_q2_splits(rows)
    assert first == second
    assert set(first) == {"run-a", "run-b", "run-c"}
    assert set(first.values()) == {"train", "validation", "test"}


def test_q2_learning_splits_keep_lineages_together() -> None:
    rows = [
        {"run_id": "a-1", "lineage_id": "a"},
        {"run_id": "a-2", "lineage_id": "a"},
        {"run_id": "b-1", "lineage_id": "b"},
        {"run_id": "c-1", "lineage_id": "c"},
    ]
    splits = grouped_q2_splits(rows)
    assert splits["a-1"] == splits["a-2"]
    assert set(splits.values()) == {"train", "validation", "test"}


def test_q2_round3_audit_absorption_and_fingerprint_are_deterministic() -> None:
    data = load_problem_data()
    best = ROOT / "outputs" / "q2" / "best"
    solution = load_q2_solution(best / "q2-routes.csv", best / "q2-assignments.csv", data)
    route_rows, pair_rows = audit_q2_solution(solution, data)
    ranking = absorption_potential_ranking(route_rows, pair_rows)
    assert len(route_rows) == 96
    assert len(pair_rows) == 96 * 95
    assert len(ranking) == 96
    assert sorted(int(row["absorption_rank"]) for row in ranking) == list(range(1, 97))
    source = int(ranking[0]["route_index"])
    neighborhood = select_absorption_neighborhood(source, pair_rows, route_count=6)
    assert neighborhood[0] == source
    assert len(neighborhood) == len(set(neighborhood)) == 6
    assert q2_basin_fingerprint(solution) == q2_basin_fingerprint(solution)


def test_q2_promising_master_queue_requires_gap_or_structure() -> None:
    queue = PromisingLocalMasterQueue()
    from src.solver.q2_lns import Q2LocalRepair

    assert not queue.add_from_repair(Q2LocalRepair(None, {"primary_mip_gap": 0.0, "destroyed_routes": [1, 2]}))
    assert queue.add_from_repair(
        Q2LocalRepair(
            None,
            {
                "primary_mip_gap": 0.05,
                "primary_dual_bound": 500.0,
                "primary_status": 1,
                "after_aircraft_minutes": 550,
                "destroyed_routes": [1, 2, 3, 4],
                "candidate_pool_hash": "abc",
                "selected_new_candidates": 1,
            },
        )
    )
    assert len(queue.entries) == 1


def test_q2_solution_diversity_is_symmetric_and_zero_for_identity() -> None:
    data = load_problem_data()
    best = ROOT / "outputs" / "q2" / "best"
    control = ROOT / "outputs" / "q2" / "runs" / "20260815-q2-alns-seed2"
    left = load_q2_solution(best / "q2-routes.csv", best / "q2-assignments.csv", data)
    right = load_q2_solution(
        control / "q2-routes.csv", control / "q2-assignments.csv", data
    )
    assert q2_solution_diversity(left, left) == 0.0
    assert q2_solution_diversity(left, right) == q2_solution_diversity(right, left)
    assert q2_solution_diversity(left, right) > 0.0


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


def test_q2_directed_flow_graph_preserves_direction_and_land_flexibility() -> None:
    data = load_problem_data()
    graph = build_q2_directed_flow_graph(data)
    assert len(graph.nodes) == 55
    assert sum(graph.directed_demand.values()) + sum(graph.land_outbound.values()) + sum(
        graph.land_inbound.values()
    ) == 4000
    assert sum(graph.shuttle_demand.values()) == 800
    assert any(
        graph.shuttle_demand.get((right, left), 0) != value
        for (left, right), value in graph.shuttle_demand.items()
    )
    assert "LAND" not in graph.nodes


def test_q2_flow_candidates_are_bounded_deterministic_and_include_long_routes() -> None:
    data = load_problem_data()
    baseline = ROOT / "outputs" / "q2" / "baseline-19736"
    solution = load_q2_solution(
        baseline / "q2-routes.csv",
        baseline / "q2-assignments.csv",
        data,
    )
    routes = solution.routes[90:93]
    local = build_q2_local_data(data, routes)
    graph = build_q2_directed_flow_graph(data)
    first, first_features = flow_aware_local_sequences(
        local, routes, graph, max_sequence_length=5, budget=24
    )
    second, second_features = flow_aware_local_sequences(
        local, routes, graph, max_sequence_length=5, budget=24
    )
    assert first == second
    assert first_features == second_features
    assert len(first) == 24
    assert any(len(sequence) >= 3 for sequence in first)
    assert all(len(set(sequence)) == len(sequence) for sequence in first)
