from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import DEFAULT_CONFIG_PATH
from src.io_utils import read_csv, sha256, write_csv, write_json
from src.solver import (
    ClusterCandidateRanker,
    RawDistanceRanker,
    RelatednessModel,
    cluster_sweep,
    export_q1_solution,
    improve_q1_savings,
    load_fuel_signatures,
    load_problem_data,
    solve_q1_baseline,
)
from src.solver.clustering import ClusterResult
from src.solver.models import Solution, SolverConfig
from src.validation import validate_solution


EVENT_FIELDS = (
    "iteration",
    "ranker",
    "candidate_rank",
    "selected",
    "left_index",
    "right_index",
    "base_airport",
    "left_signature",
    "right_signature",
    "left_load",
    "right_load",
    "left_services",
    "right_services",
    "same_cluster_fraction",
    "minimum_distance_km",
    "airport_profile_gap_km",
    "fuel_signature_gap",
    "outcome",
    "route_evaluations",
    "technical_stop_searches",
    "augmentation_cache_hits",
    "lower_bound_pruned",
    "augmentation_infeasible",
    "saving_minutes",
    "accepted",
)


@dataclass(frozen=True)
class ExperimentSpec:
    name: str
    ranker: object
    candidate_mode: str
    pair_budget: int | None
    cluster_fit_seconds: float = 0.0


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
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


def _comparison_key(metrics: dict[str, object]) -> tuple[float, ...]:
    return (
        float(metrics["total_aircraft_time_minutes"]),
        float(metrics["total_passenger_travel_time_minutes"]),
        float(metrics["total_flights"]),
        float(metrics["total_fuel_consumption_kg"]),
        -float(metrics["seat_utilization"]),
    )


def _cluster_summary_row(result: ClusterResult) -> dict[str, object]:
    return {
        "method": result.method,
        "k": result.k,
        "silhouette": result.silhouette,
        "within_dissimilarity": result.within_dissimilarity,
        "minimum_cluster_size": min(result.cluster_sizes),
        "maximum_cluster_size": max(result.cluster_sizes),
        "cluster_sizes": "|".join(map(str, result.cluster_sizes)),
        "medoids": "|".join(result.medoids),
        "stability_median_ari": result.stability_median_ari,
        "passes_internal_screen": (
            min(result.cluster_sizes) > 1
            and (result.stability_median_ari or 0.0) >= 0.80
        ),
    }


def _run_solution(
    *,
    spec: ExperimentSpec,
    baseline: Solution,
    data,
    solver_config: SolverConfig,
    output_dir: Path,
    max_iterations: int,
    max_neighbors: int,
    write_artifacts: bool,
) -> tuple[dict[str, object], Solution]:
    events: list[dict[str, object]] | None = [] if write_artifacts else None
    started = time.perf_counter()
    solution = improve_q1_savings(
        baseline,
        data,
        solver_config,
        max_neighbors=max_neighbors,
        max_iterations=max_iterations,
        candidate_ranker=spec.ranker,
        candidate_mode=spec.candidate_mode,
        pair_budget=spec.pair_budget,
        candidate_events=events,
    )
    solve_seconds = time.perf_counter() - started
    stats = solution.diagnostics["generalized_savings"]
    validator_metrics = solution.metrics.to_dict()
    gate_pass = True
    metrics_match = True
    if write_artifacts:
        output_dir.mkdir(parents=True, exist_ok=False)
        routes_path = output_dir / "q1-routes.csv"
        assignments_path = output_dir / "q1-assignments.csv"
        export_q1_solution(solution, routes_path, assignments_path)
        validation = validate_solution(
            "q1", routes_path, assignments_path, data_dir=ROOT / "data" / "raw", config=data.config
        )
        validator_metrics = validation.metrics.to_dict() if validation.metrics else {}
        metrics_match = bool(
            validator_metrics
            and all(
                abs(float(validator_metrics[key]) - float(solution.metrics.to_dict()[key])) <= 1e-6
                for key in solution.metrics.to_dict()
            )
        )
        gate_pass = bool(
            validation.valid
            and metrics_match
            and validator_metrics.get("served_passengers") == data.q1_passenger_count
        )
        write_json(output_dir / "validator.json", validation.to_dict())
        write_json(
            output_dir / "metrics.json",
            {
                "gate_pass": gate_pass,
                "metrics_match": metrics_match,
                "internal_metrics": solution.metrics.to_dict(),
                "validator_metrics": validator_metrics,
            },
        )
        write_json(
            output_dir / "run_config.json",
            {
                "name": spec.name,
                "ranker": stats["ranker"],
                "candidate_mode": spec.candidate_mode,
                "pair_budget": spec.pair_budget,
                "max_iterations": max_iterations,
                "max_neighbors": max_neighbors,
                "cluster_fit_seconds": spec.cluster_fit_seconds,
                "solve_seconds": solve_seconds,
                "git_commit": _git_commit(),
                "git_dirty": _git_dirty(),
                "problem_config_sha256": sha256(DEFAULT_CONFIG_PATH),
                "diagnostics": solution.diagnostics,
            },
        )
        assert events is not None
        write_csv(output_dir / "candidate_events.csv", EVENT_FIELDS, events)

    row: dict[str, object] = {
        "name": spec.name,
        "ranker": stats["ranker"],
        "candidate_mode": spec.candidate_mode,
        "pair_budget": "all" if spec.pair_budget is None else spec.pair_budget,
        **validator_metrics,
        **{f"search_{key}": value for key, value in stats.items()},
        "cluster_fit_seconds": round(spec.cluster_fit_seconds, 6),
        "solve_seconds": round(solve_seconds, 6),
        "end_to_end_seconds": round(solve_seconds + spec.cluster_fit_seconds, 6),
        "gate_pass": gate_pass,
        "metrics_match": metrics_match,
    }
    return row, solution


def _select_best(rows: list[dict[str, object]], method: str, tuning_budget: int) -> dict[str, object]:
    candidates = [
        row
        for row in rows
        if str(row["ranker"]).startswith(f"cluster_{method}_")
        and str(row["pair_budget"]) == str(tuning_budget)
        and row["gate_pass"]
    ]
    if not candidates:
        raise RuntimeError(f"no valid downstream result for {method}")
    return min(
        candidates,
        key=lambda row: (
            _comparison_key(row),
            int(row["search_evaluated_routes"]),
            float(row["end_to_end_seconds"]),
            str(row["ranker"]),
        ),
    )


def _decision_rows(
    rows: list[dict[str, object]],
    selected_rankers: list[str],
    budgets: tuple[int, ...],
    tuning_budget: int,
) -> list[dict[str, object]]:
    decisions: list[dict[str, object]] = []
    for ranker in selected_rankers:
        comparisons = []
        for budget in budgets:
            raw = next(
                row
                for row in rows
                if row["ranker"] == "raw_distance" and str(row["pair_budget"]) == str(budget)
            )
            treatment = next(
                row
                for row in rows
                if row["ranker"] == ranker and str(row["pair_budget"]) == str(budget)
            )
            comparisons.append((raw, treatment))
        better = sum(
            int(
                int(treatment["total_aircraft_time_minutes"])
                < int(raw["total_aircraft_time_minutes"])
            )
            for raw, treatment in comparisons
        )
        never_worse = all(
            int(treatment["total_aircraft_time_minutes"])
            <= int(raw["total_aircraft_time_minutes"])
            for raw, treatment in comparisons
        )
        tuning_raw, tuning_treatment = comparisons[budgets.index(tuning_budget)]
        equal_quality = _comparison_key(tuning_treatment) == _comparison_key(tuning_raw)
        evaluation_reduction = 1 - float(tuning_treatment["search_evaluated_routes"]) / max(
            1, float(tuning_raw["search_evaluated_routes"])
        )
        treatment_runtime = float(
            tuning_treatment.get("median_end_to_end_seconds")
            or tuning_treatment["end_to_end_seconds"]
        )
        raw_runtime = float(
            tuning_raw.get("median_end_to_end_seconds") or tuning_raw["end_to_end_seconds"]
        )
        runtime_reduction = 1 - treatment_runtime / max(
            1e-9, raw_runtime
        )
        if better >= 2 and never_worse:
            decision = "promote_quality"
        elif equal_quality and evaluation_reduction >= 0.20 and runtime_reduction >= 0.15:
            decision = "promote_efficiency"
        elif never_worse and (evaluation_reduction >= 0.10 or runtime_reduction >= 0.10):
            decision = "auxiliary_relatedness"
        else:
            decision = "abandon_mainline"
        decisions.append(
            {
                "ranker": ranker,
                "better_budget_count": better,
                "never_worse": never_worse,
                "tuning_equal_lexicographic_quality": equal_quality,
                "tuning_route_evaluation_reduction": evaluation_reduction,
                "tuning_runtime_reduction": runtime_reduction,
                "decision": decision,
            }
        )
    return decisions


def main() -> int:
    parser = argparse.ArgumentParser(description="Q1 clustering Phase-1 controlled experiments")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "q1" / "clustering")
    parser.add_argument("--k-min", type=int, default=2)
    parser.add_argument("--k-max", type=int, default=10)
    parser.add_argument("--budgets", default="25,50,75")
    parser.add_argument("--tuning-budget", type=int, default=50)
    parser.add_argument("--stability-repeats", type=int, default=50)
    parser.add_argument("--timing-repeats", type=int, default=1)
    parser.add_argument("--max-iterations", type=int, default=100)
    parser.add_argument("--max-neighbors", type=int, default=5)
    parser.add_argument("--full", action="store_true", help="run all K at tuning budget plus confirmations and full audits")
    parser.add_argument("--resume", action="store_true", help="reuse completed runs in an existing --run-id")
    args = parser.parse_args()

    budgets = tuple(int(value) for value in args.budgets.split(",") if value)
    if args.tuning_budget not in budgets:
        raise ValueError("tuning budget must be present in --budgets")
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S-phase1")
    run_dir = args.output_root / run_id
    if run_dir.exists() and not args.resume:
        raise FileExistsError(f"experiment directory already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=args.resume)

    data = load_problem_data()
    solver_config = SolverConfig(seed=0)
    facilities = tuple(data.config.facilities)
    fuel_signatures = load_fuel_signatures(
        ROOT / "data" / "processed" / "features" / "closed_route_reachability.csv"
    )

    cluster_started = time.perf_counter()
    cluster_results = cluster_sweep(
        facilities,
        data.matrix,
        k_values=range(args.k_min, args.k_max + 1),
        stability_repeats=args.stability_repeats,
        seed=solver_config.seed,
    )
    total_cluster_seconds = time.perf_counter() - cluster_started
    fit_seconds = total_cluster_seconds / len(cluster_results)
    summary_rows = [_cluster_summary_row(result) for result in cluster_results]
    write_csv(
        run_dir / "cluster_summary.csv",
        tuple(summary_rows[0]),
        summary_rows,
    )
    membership_rows = [row for result in cluster_results for row in result.membership_rows()]
    write_csv(run_dir / "facility_clusters.csv", tuple(membership_rows[0]), membership_rows)

    baseline_started = time.perf_counter()
    baseline = solve_q1_baseline(data, solver_config)
    baseline_seconds = time.perf_counter() - baseline_started

    all_rows: list[dict[str, object]] = []
    if args.resume and (run_dir / "ab_summary.csv").exists():
        all_rows = list(read_csv(run_dir / "ab_summary.csv"))
        for row in all_rows:
            row["gate_pass"] = str(row["gate_pass"]).lower() == "true"
            row["metrics_match"] = str(row["metrics_match"]).lower() == "true"
    solutions: dict[str, Solution] = {}
    specs: dict[str, ExperimentSpec] = {}
    completed_names = {str(row["name"]) for row in all_rows}

    def execute(spec: ExperimentSpec, *, repeats: int = 1) -> None:
        specs[spec.name] = spec
        if spec.name in completed_names:
            return
        timing_values: list[float] = []
        for repeat in range(repeats):
            artifact_dir = run_dir / "runs" / spec.name
            row, solution = _run_solution(
                spec=spec,
                baseline=baseline,
                data=data,
                solver_config=solver_config,
                output_dir=artifact_dir,
                max_iterations=args.max_iterations,
                max_neighbors=args.max_neighbors,
                write_artifacts=repeat == 0,
            )
            timing_values.append(float(row["end_to_end_seconds"]))
            if repeat == 0:
                all_rows.append(row)
                solutions[spec.name] = solution
                completed_names.add(spec.name)
        first = all_rows[-1]
        first["timing_repeats"] = repeats
        first["median_end_to_end_seconds"] = median(timing_values)
        first["minimum_end_to_end_seconds"] = min(timing_values)
        first["maximum_end_to_end_seconds"] = max(timing_values)

    raw_ranker = RawDistanceRanker(fuel_signatures)
    legacy = ExperimentSpec("legacy-reproduction", raw_ranker, "legacy", None)
    execute(legacy)
    legacy_row = next(row for row in all_rows if row["name"] == legacy.name)
    reproduction_pass = bool(
        int(legacy_row["total_aircraft_time_minutes"]) == 15743
        and int(legacy_row["search_evaluated_pairs"]) == 738
        and int(legacy_row["search_evaluated_routes"]) == 2112
        and int(legacy_row["search_accepted_merges"]) == 10
    )

    for budget in budgets:
        execute(ExperimentSpec(f"raw-b{budget}", raw_ranker, "global", budget))

    screened = [
        result
        for result, row in zip(cluster_results, summary_rows)
        if row["passes_internal_screen"]
    ]
    if not screened:
        raise RuntimeError("no clustering configuration passed the internal screen")
    tuning_results = screened if args.full else [
        max(
            (result for result in screened if result.method == method),
            key=lambda result: (result.silhouette, -result.k),
        )
        for method in ("pam", "average")
    ]
    for result in tuning_results:
        model = RelatednessModel(
            result, data.matrix, tuple(data.config.airports), fuel_signatures
        )
        ranker = ClusterCandidateRanker(model)
        execute(
            ExperimentSpec(
                f"{ranker.name}-b{args.tuning_budget}",
                ranker,
                "global",
                args.tuning_budget,
                fit_seconds,
            )
        )

    selected_rows = [
        _select_best(all_rows, method, args.tuning_budget) for method in ("pam", "average")
    ]
    selected_rankers = [str(row["ranker"]) for row in selected_rows]
    selected_results = {
        f"cluster_{result.method}_k{result.k}": result for result in cluster_results
    }
    for ranker_name in selected_rankers:
        result = selected_results[ranker_name]
        ranker = ClusterCandidateRanker(
            RelatednessModel(result, data.matrix, tuple(data.config.airports), fuel_signatures)
        )
        for budget in budgets:
            name = f"{ranker.name}-b{budget}"
            if name not in solutions:
                execute(
                    ExperimentSpec(name, ranker, "global", budget, fit_seconds)
                )

    if args.full:
        execute(ExperimentSpec("raw-full", raw_ranker, "global", None))
        for ranker_name in selected_rankers:
            result = selected_results[ranker_name]
            ranker = ClusterCandidateRanker(
                RelatednessModel(result, data.matrix, tuple(data.config.airports), fuel_signatures)
            )
            execute(
                ExperimentSpec(f"{ranker.name}-full", ranker, "global", None, fit_seconds)
            )

    if args.timing_repeats > 1:
        timing_names = [f"raw-b{args.tuning_budget}"] + [
            f"{ranker}-b{args.tuning_budget}" for ranker in selected_rankers
        ]
        for name in timing_names:
            original = next(row for row in all_rows if row["name"] == name)
            times = [float(original["end_to_end_seconds"])]
            for _ in range(args.timing_repeats - 1):
                row, _ = _run_solution(
                    spec=specs[name],
                    baseline=baseline,
                    data=data,
                    solver_config=solver_config,
                    output_dir=run_dir / "unused",
                    max_iterations=args.max_iterations,
                    max_neighbors=args.max_neighbors,
                    write_artifacts=False,
                )
                times.append(float(row["end_to_end_seconds"]))
            original["timing_repeats"] = len(times)
            original["median_end_to_end_seconds"] = median(times)
            original["minimum_end_to_end_seconds"] = min(times)
            original["maximum_end_to_end_seconds"] = max(times)

    decisions = _decision_rows(all_rows, selected_rankers, budgets, args.tuning_budget)
    if args.full:
        raw_full = next(row for row in all_rows if row["name"] == "raw-full")
        for decision in decisions:
            treatment_full = next(
                row
                for row in all_rows
                if row["ranker"] == decision["ranker"] and row["pair_budget"] == "all"
            )
            decision["full_audit_invariant"] = (
                _comparison_key(raw_full) == _comparison_key(treatment_full)
            )

    result_fields = tuple(dict.fromkeys(key for row in all_rows for key in row))
    write_csv(run_dir / "ab_summary.csv", result_fields, all_rows)
    write_csv(run_dir / "decision_summary.csv", tuple(decisions[0]), decisions)
    write_json(
        run_dir / "experiment_config.json",
        {
            "run_id": run_id,
            "git_commit": _git_commit(),
            "git_dirty": _git_dirty(),
            "problem_config_sha256": sha256(DEFAULT_CONFIG_PATH),
            "k_range": [args.k_min, args.k_max],
            "budgets": budgets,
            "tuning_budget": args.tuning_budget,
            "stability_repeats": args.stability_repeats,
            "timing_repeats": args.timing_repeats,
            "full": args.full,
            "baseline_seconds": baseline_seconds,
            "cluster_sweep_seconds": total_cluster_seconds,
            "legacy_reproduction_pass": reproduction_pass,
            "selected_rankers": selected_rankers,
            "decisions": decisions,
        },
    )
    print(f"experiment directory: {run_dir}")
    print(f"legacy reproduction: {'PASS' if reproduction_pass else 'FAIL'}")
    print(json.dumps(decisions, ensure_ascii=False, indent=2))
    return 0 if reproduction_pass and all(row["gate_pass"] for row in all_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
