from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import DEFAULT_CONFIG_PATH
from src.io_utils import read_csv, sha256, write_csv, write_json
from src.solver import (
    ContextCompatibility,
    ContextRelatednessRanker,
    RawDistanceRanker,
    SolverCache,
    StaticRelatednessModel,
    StaticRelatednessRanker,
    build_static_components,
    consensus_coassociation,
    export_q1_solution,
    improve_q1_savings,
    load_fuel_signatures,
    load_problem_data,
    solve_q1_baseline,
)
from src.solver.relatedness import consensus_leave_one_out_deviation
from src.solver.models import SolverConfig
from src.validation import validate_solution


PHASE1_DIR = ROOT / "outputs" / "q1" / "clustering" / "20260814-phase1-screen"
PHASE1_EVENTS = PHASE1_DIR / "runs" / "raw-full" / "candidate_events.csv"
FUEL_FEATURES = ROOT / "data" / "processed" / "features" / "closed_route_reachability.csv"


@dataclass(frozen=True)
class OfflineCandidate:
    iteration: int
    candidate_id: str
    left_services: tuple[str, ...]
    right_services: tuple[str, ...]
    left_load: int
    right_load: int
    left_base: str
    right_base: str
    saving: float


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _git_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode != 0 or bool(result.stdout.strip())


def _load_phase1_consensus(facilities: Sequence[str]):
    summary = read_csv(PHASE1_DIR / "cluster_summary.csv")
    stable = {
        (row["method"], int(row["k"])): float(row["stability_median_ari"])
        for row in summary
        if row["passes_internal_screen"].lower() == "true"
    }
    grouped: dict[str, dict[str, int]] = defaultdict(dict)
    for row in read_csv(PHASE1_DIR / "facility_clusters.csv"):
        key = (row["method"], int(row["k"]))
        if key in stable:
            grouped[f"{key[0]}_k{key[1]}"][row["facility"]] = int(row["cluster"])
    weights = {
        f"{method}_k{k}": stability for (method, k), stability in stable.items()
    }
    consensus = consensus_coassociation(facilities, grouped, weights=weights)
    return consensus, grouped, weights


def _load_offline_candidates() -> tuple[OfflineCandidate, ...]:
    candidates = []
    for row in read_csv(PHASE1_EVENTS):
        if row["selected"].lower() != "true" or not row["saving_minutes"]:
            continue
        candidates.append(
            OfflineCandidate(
                iteration=int(row["iteration"]),
                candidate_id=f"{row['left_signature']}||{row['right_signature']}",
                left_services=tuple(value for value in row["left_services"].split("|") if value),
                right_services=tuple(value for value in row["right_services"].split("|") if value),
                left_load=int(row["left_load"]),
                right_load=int(row["right_load"]),
                left_base=row["base_airport"],
                right_base=row["base_airport"],
                saving=float(row["saving_minutes"]),
            )
        )
    if not candidates:
        raise RuntimeError("Phase-1 full-audit log has no exact candidate savings")
    return tuple(candidates)


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position + 1
        while end < len(order) and values[order[end]] == values[order[position]]:
            end += 1
        rank = (position + end - 1) / 2 + 1
        for index in order[position:end]:
            ranks[index] = rank
        position = end
    return ranks


def _spearman(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) < 2:
        return 1.0
    a = _average_ranks(left)
    b = _average_ranks(right)
    mean_a, mean_b = mean(a), mean(b)
    numerator = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    denominator = math.sqrt(
        sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b)
    )
    return numerator / denominator if denominator else 0.0


def _ndcg(relevances: Sequence[float], ideal: Sequence[float]) -> float:
    dcg = sum(value / math.log2(index + 2) for index, value in enumerate(relevances))
    idcg = sum(value / math.log2(index + 2) for index, value in enumerate(ideal))
    return dcg / idcg if idcg else 1.0


def _evaluate_offline(
    name: str,
    candidates: Sequence[OfflineCandidate],
    scorer: Callable[[OfflineCandidate], float],
    *,
    ks: Sequence[int] = (25, 50),
) -> tuple[dict[str, object], list[dict[str, object]]]:
    grouped: dict[int, list[OfflineCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.iteration].append(candidate)
    iteration_rows: list[dict[str, object]] = []
    for iteration, values in sorted(grouped.items()):
        scored = [(candidate, scorer(candidate)) for candidate in values]
        predicted = sorted(scored, key=lambda item: (-item[1], item[0].candidate_id))
        actual = sorted(values, key=lambda item: (-item.saving, item.candidate_id))
        row: dict[str, object] = {
            "model": name,
            "iteration": iteration,
            "candidate_count": len(values),
            "spearman": _spearman(
                [score for _, score in scored], [candidate.saving for candidate, _ in scored]
            ),
        }
        for requested_k in ks:
            k = min(requested_k, len(values))
            predicted_top = [candidate for candidate, _ in predicted[:k]]
            actual_top = actual[:k]
            actual_ids = {candidate.candidate_id for candidate in actual_top}
            predicted_relevance = [max(0.0, candidate.saving) for candidate in predicted_top]
            ideal_relevance = [max(0.0, candidate.saving) for candidate in actual_top]
            row[f"recall_at_{requested_k}"] = sum(
                candidate.candidate_id in actual_ids for candidate in predicted_top
            ) / k
            row[f"ndcg_at_{requested_k}"] = _ndcg(
                predicted_relevance, ideal_relevance
            )
            ideal_total = sum(ideal_relevance)
            row[f"high_saving_coverage_at_{requested_k}"] = (
                sum(predicted_relevance) / ideal_total if ideal_total else 1.0
            )
            row[f"best_move_hit_at_{requested_k}"] = float(
                actual[0].candidate_id
                in {candidate.candidate_id for candidate in predicted_top}
            )
        iteration_rows.append(row)
    summary: dict[str, object] = {
        "model": name,
        "iterations": len(iteration_rows),
        "candidate_count": len(candidates),
    }
    for field in iteration_rows[0]:
        if field not in {"model", "iteration", "candidate_count"}:
            summary[field] = mean(float(row[field]) for row in iteration_rows)
    return summary, iteration_rows


def _component_rows(components) -> list[dict[str, object]]:
    rows = []
    facilities = components.facilities
    for index, left in enumerate(facilities):
        for right in facilities[index + 1 :]:
            evidence = components.capacity_evidence[tuple(sorted((left, right)))]
            rows.append(
                {
                    "left": left,
                    "right": right,
                    **{
                        name: matrix[left][right]
                        for name, matrix in components.matrices.items()
                    },
                    "left_demand": evidence.left_demand,
                    "right_demand": evidence.right_demand,
                    "separate_seats": evidence.separate_seats,
                    "combined_seats": evidence.combined_seats,
                    "saved_seats": evidence.saved_seats,
                    "combined_utilization": evidence.combined_utilization,
                }
            )
    return rows


def _run_downstream(
    name: str,
    ranker,
    data,
    output_dir: Path,
    *,
    pair_budget: int,
) -> dict[str, object]:
    cache = SolverCache(data)
    solver_config = SolverConfig(seed=0)
    started = time.perf_counter()
    baseline = solve_q1_baseline(data, solver_config, cache=cache)
    baseline_seconds = time.perf_counter() - started
    events: list[dict[str, object]] = []
    search_started = time.perf_counter()
    solution = improve_q1_savings(
        baseline,
        data,
        solver_config,
        candidate_ranker=ranker,
        candidate_mode="global",
        pair_budget=pair_budget,
        max_iterations=100,
        candidate_events=events,
        cache=cache,
    )
    search_seconds = time.perf_counter() - search_started
    elapsed_seconds = time.perf_counter() - started
    output_dir.mkdir(parents=True, exist_ok=False)
    routes_path = output_dir / "q1-routes.csv"
    assignments_path = output_dir / "q1-assignments.csv"
    export_q1_solution(solution, routes_path, assignments_path)
    validation = validate_solution(
        "q1",
        routes_path,
        assignments_path,
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    validator_metrics = validation.metrics.to_dict() if validation.metrics else {}
    internal_metrics = solution.metrics.to_dict()
    metrics_match = bool(
        validator_metrics
        and all(
            abs(float(validator_metrics[key]) - float(internal_metrics[key])) <= 1e-6
            for key in internal_metrics
        )
    )
    gate_pass = bool(
        validation.valid
        and metrics_match
        and validator_metrics.get("served_passengers") == data.q1_passenger_count
    )
    selected_events = [event for event in events if event["selected"]]
    improving_events = [
        event
        for event in selected_events
        if event["saving_minutes"] != "" and float(event["saving_minutes"]) > 0
    ]
    accepted_events = [event for event in selected_events if event["accepted"]]
    first_improvement = min(
        (
            float(event["evaluated_elapsed_seconds"])
            for event in improving_events
            if event["evaluated_elapsed_seconds"] != ""
        ),
        default=None,
    )
    time_to_best = max(
        (
            float(event["accepted_elapsed_seconds"])
            for event in accepted_events
            if event["accepted_elapsed_seconds"] != ""
        ),
        default=None,
    )
    stats = solution.diagnostics["generalized_savings"]
    row = {
        "name": name,
        "ranker": ranker.name,
        "pair_budget": pair_budget,
        **validator_metrics,
        "candidate_evaluations": stats["evaluated_pairs"],
        "route_evaluations": stats["evaluated_routes"],
        "high_value_moves_found_ge_100min": sum(
            float(event["saving_minutes"]) >= 100
            for event in selected_events
            if event["saving_minutes"] != ""
        ),
        "accepted_moves": stats["accepted_merges"],
        "primary_improvement_minutes": stats["primary_improvement_minutes"],
        "improvement_per_route_evaluation": stats["primary_improvement_minutes"]
        / max(1, stats["evaluated_routes"]),
        "baseline_seconds": baseline_seconds,
        "search_seconds": search_seconds,
        "elapsed_seconds": elapsed_seconds,
        "time_to_first_improvement_seconds": first_improvement,
        "time_to_best_seconds": time_to_best,
        "gate_pass": gate_pass,
        "metrics_match": metrics_match,
    }
    event_fields = tuple(dict.fromkeys(key for event in events for key in event))
    write_csv(output_dir / "candidate_events.csv", event_fields, events)
    write_json(output_dir / "validator.json", validation.to_dict())
    write_json(
        output_dir / "metrics.json",
        {
            "gate_pass": gate_pass,
            "metrics_match": metrics_match,
            "internal_metrics": internal_metrics,
            "validator_metrics": validator_metrics,
        },
    )
    write_json(
        output_dir / "run_config.json",
        {
            "name": name,
            "ranker": ranker.name,
            "pair_budget": pair_budget,
            "git_commit": _git_commit(),
            "git_dirty": _git_dirty(),
            "problem_config_sha256": sha256(DEFAULT_CONFIG_PATH),
            "performance": cache.stats(),
            "search_diagnostics": stats,
            "timing": {
                "baseline_seconds": baseline_seconds,
                "search_seconds": search_seconds,
                "elapsed_seconds": elapsed_seconds,
                "time_to_first_improvement_seconds": first_improvement,
                "time_to_best_seconds": time_to_best,
            },
        },
    )
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="Q1 Relatedness Phase-2 experiment")
    parser.add_argument("--run-id", default="20260815-phase2")
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "outputs" / "q1" / "relatedness"
    )
    parser.add_argument("--pair-budget", type=int, default=25)
    args = parser.parse_args()
    run_dir = args.output_root / args.run_id
    if run_dir.exists():
        raise FileExistsError(f"experiment directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    data = load_problem_data()
    facilities = tuple(data.config.facilities)
    fuel_signatures = load_fuel_signatures(FUEL_FEATURES)
    consensus, labelings, consensus_weights = _load_phase1_consensus(facilities)
    components = build_static_components(
        data, consensus=consensus.matrix, fuel_signatures=fuel_signatures
    )
    component_rows = _component_rows(components)
    write_csv(run_dir / "relatedness_components.csv", tuple(component_rows[0]), component_rows)
    leave_one_out = consensus_leave_one_out_deviation(consensus, labelings)
    write_json(
        run_dir / "consensus_diagnostics.json",
        {
            **consensus.to_dict(),
            "stability_weights": consensus_weights,
            "leave_one_out_mean_absolute_deviation": leave_one_out,
            "maximum_leave_one_out_deviation": max(leave_one_out.values()),
        },
    )

    candidates = _load_offline_candidates()
    models = {
        "raw_distance": StaticRelatednessModel.equal_weighted(
            components, ("distance",), name="raw_distance_percentile"
        ),
        "distance_consensus": StaticRelatednessModel.equal_weighted(
            components, ("distance", "consensus"), name="static_distance_consensus"
        ),
        "distance_airport": StaticRelatednessModel.equal_weighted(
            components, ("distance", "airport"), name="static_distance_airport"
        ),
        "distance_fuel": StaticRelatednessModel.equal_weighted(
            components, ("distance", "fuel"), name="static_distance_fuel"
        ),
        "distance_capacity": StaticRelatednessModel.equal_weighted(
            components, ("distance", "capacity"), name="static_distance_capacity"
        ),
        "full_static": StaticRelatednessModel.equal_weighted(
            components,
            ("distance", "consensus", "airport", "fuel", "capacity"),
            name="static_full_equal_rank",
        ),
    }

    def model_score(model: StaticRelatednessModel, candidate: OfflineCandidate) -> float:
        return model.route_pair_features(
            candidate.left_services, candidate.right_services
        ).score

    offline_summaries: list[dict[str, object]] = []
    offline_iterations: list[dict[str, object]] = []
    summary_by_name: dict[str, dict[str, object]] = {}
    for name, model in models.items():
        summary, rows = _evaluate_offline(
            name, candidates, lambda candidate, model=model: model_score(model, candidate)
        )
        offline_summaries.append(summary)
        offline_iterations.extend(rows)
        summary_by_name[name] = summary

    hard_labels = labelings["pam_k3"]

    def hard_score(candidate: OfflineCandidate) -> float:
        pairs = [
            (left, right)
            for left in candidate.left_services
            for right in candidate.right_services
        ]
        same_fraction = mean(float(hard_labels[left] == hard_labels[right]) for left, right in pairs)
        distance_tie = max(
            components.matrices["distance"][left][right] for left, right in pairs
        )
        return same_fraction + distance_tie * 1e-3

    hard_summary, hard_rows = _evaluate_offline("hard_pam_k3", candidates, hard_score)
    offline_summaries.append(hard_summary)
    offline_iterations.extend(hard_rows)
    summary_by_name["hard_pam_k3"] = hard_summary

    raw_summary = summary_by_name["raw_distance"]
    raw_iteration = {
        int(row["iteration"]): row
        for row in offline_iterations
        if row["model"] == "raw_distance"
    }
    decisions = []
    adopted = []
    for component in ("consensus", "airport", "fuel", "capacity"):
        name = f"distance_{component}"
        summary = summary_by_name[name]
        rows = [row for row in offline_iterations if row["model"] == name]
        wins = sum(
            float(row["ndcg_at_25"])
            > float(raw_iteration[int(row["iteration"])]["ndcg_at_25"]) + 1e-12
            for row in rows
        )
        losses = sum(
            float(row["ndcg_at_25"]) + 1e-12
            < float(raw_iteration[int(row["iteration"])]["ndcg_at_25"])
            for row in rows
        )
        if (
            float(summary["ndcg_at_25"]) > float(raw_summary["ndcg_at_25"])
            and float(summary["ndcg_at_50"]) >= float(raw_summary["ndcg_at_50"])
            and wins > losses
        ):
            decision = "ADOPT"
            adopted.append(component)
        elif (
            float(summary["ndcg_at_25"]) > float(raw_summary["ndcg_at_25"])
            or float(summary["high_saving_coverage_at_25"])
            > float(raw_summary["high_saving_coverage_at_25"])
        ) and float(summary["ndcg_at_25"]) >= float(raw_summary["ndcg_at_25"]) - 0.01:
            decision = "OPTIONAL"
        else:
            decision = "REJECT"
        decisions.append(
            {
                "component": component,
                "model": name,
                "decision": decision,
                "ndcg25_delta_vs_raw": float(summary["ndcg_at_25"])
                - float(raw_summary["ndcg_at_25"]),
                "ndcg50_delta_vs_raw": float(summary["ndcg_at_50"])
                - float(raw_summary["ndcg_at_50"]),
                "coverage25_delta_vs_raw": float(summary["high_saving_coverage_at_25"])
                - float(raw_summary["high_saving_coverage_at_25"]),
                "iteration_wins": wins,
                "iteration_losses": losses,
            }
        )

    selected_components = ("distance", *adopted)
    selected_model = StaticRelatednessModel.equal_weighted(
        components, selected_components, name="static_selected_" + "_".join(selected_components)
    )
    if selected_model.name not in {model.name for model in models.values()}:
        selected_summary, selected_rows = _evaluate_offline(
            "selected_static",
            candidates,
            lambda candidate: model_score(selected_model, candidate),
        )
        offline_summaries.append(selected_summary)
        offline_iterations.extend(selected_rows)
        summary_by_name["selected_static"] = selected_summary
    else:
        selected_name = next(
            name for name, model in models.items() if model.name == selected_model.name
        )
        summary_by_name["selected_static"] = summary_by_name[selected_name]

    context = ContextCompatibility(selected_model)

    def context_score(candidate: OfflineCandidate) -> float:
        static_score = model_score(selected_model, candidate)
        context_features = context.route_pair_from_values(
            candidate.left_services,
            candidate.right_services,
            candidate.left_load,
            candidate.right_load,
            candidate.left_base,
            candidate.right_base,
            data,
        )
        return mean((static_score, context_features.score))

    context_summary, context_rows = _evaluate_offline(
        "selected_static_context", candidates, context_score
    )
    offline_summaries.append(context_summary)
    offline_iterations.extend(context_rows)
    summary_by_name["selected_static_context"] = context_summary

    write_csv(run_dir / "offline_ranking_summary.csv", tuple(offline_summaries[0]), offline_summaries)
    write_csv(
        run_dir / "offline_iteration_metrics.csv",
        tuple(offline_iterations[0]),
        offline_iterations,
    )
    write_csv(run_dir / "ablation_summary.csv", tuple(decisions[0]), decisions)

    downstream_specs = [
        ("raw_distance", RawDistanceRanker(fuel_signatures)),
        ("best_static", StaticRelatednessRanker(selected_model)),
        (
            "best_static_context",
            ContextRelatednessRanker(
                StaticRelatednessRanker(selected_model), context, context_weight=0.5
            ),
        ),
    ]
    downstream_rows = [
        _run_downstream(
            name,
            ranker,
            data,
            run_dir / "runs" / name,
            pair_budget=args.pair_budget,
        )
        for name, ranker in downstream_specs
    ]
    write_csv(
        run_dir / "downstream_ab_summary.csv",
        tuple(downstream_rows[0]),
        downstream_rows,
    )
    write_json(
        run_dir / "experiment_config.json",
        {
            "run_id": args.run_id,
            "git_commit": _git_commit(),
            "git_dirty": _git_dirty(),
            "problem_config_sha256": sha256(DEFAULT_CONFIG_PATH),
            "phase1_source": str(PHASE1_DIR.relative_to(ROOT)),
            "stable_consensus_configurations": list(consensus.configuration_names),
            "consensus_weights": consensus_weights,
            "offline_exact_candidates": len(candidates),
            "offline_iterations": len(set(candidate.iteration for candidate in candidates)),
            "component_decisions": decisions,
            "selected_static_components": list(selected_components),
            "selected_static_weights": dict(selected_model.weights),
            "context_weight": 0.5,
            "downstream_pair_budget": args.pair_budget,
            "downstream_models": [name for name, _ in downstream_specs],
        },
    )
    print(f"Phase-2 output: {run_dir}")
    print(json.dumps(decisions, ensure_ascii=False, indent=2))
    print(json.dumps(downstream_rows, ensure_ascii=False, indent=2))
    return 0 if all(row["gate_pass"] for row in downstream_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
