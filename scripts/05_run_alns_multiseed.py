"""Multi-seed Standard ALNS experiment runner (Q1).

Runs the unchanged ALNS engine from an arbitrary valid initial solution across
multiple seeds, in either a fixed-iteration or a fixed wall-clock budget mode,
and records the full per-seed statistics plus an aggregate summary.

The engine (src/solver/alns.py) is NOT modified by this script; the only
instrumentation hook is a counting wrapper around ``evaluate_route``.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_csv, write_json
from src.solver import (
    Q1ALNSConfig,
    SolverCache,
    export_q1_solution,
    improve_q1_alns,
    load_problem_data,
    load_q1_solution,
)
from src.solver.models import SolverConfig
from src.solver.relatedness import FrozenConsensus
from src.validation import validate_solution

import src.solver.alns as _alns_mod

_EVAL_CALLS = {"count": 0}
_ORIG_EVALUATE = _alns_mod.evaluate_route


def _counting_evaluate(route, matrix, config):
    _EVAL_CALLS["count"] += 1
    return _ORIG_EVALUATE(route, matrix=matrix, config=config)


_alns_mod.evaluate_route = _counting_evaluate


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _comparison_key(metrics: dict[str, object]) -> tuple[float, ...]:
    return (
        float(metrics["total_aircraft_time_minutes"]),
        float(metrics["total_passenger_travel_time_minutes"]),
        float(metrics["total_flights"]),
        float(metrics["total_fuel_consumption_kg"]),
        -float(metrics["seat_utilization"]),
    )


def _stage_configs(args: argparse.Namespace, seed: int) -> tuple[Q1ALNSConfig, Q1ALNSConfig]:
    if args.mode == "iteration":
        stage1 = Q1ALNSConfig(
            iterations=args.stage_iterations,
            time_limit_seconds=10.0**9,
            min_destroy_routes=args.destroy_min_1,
            max_destroy_routes=args.destroy_max_1,
            max_service_nodes=2,
            repair_time_limit_seconds=args.repair_time_limit_1,
            seed=seed,
            initial_temperature=args.temperature,
            cooling_rate=args.cooling_rate,
            reaction_factor=args.reaction_factor,
            segment_length=args.segment_length,
            related_destroy_mode=args.related_destroy_mode,
            frozen_consensus=args.frozen_consensus,
            context_repair_mode=args.context_repair_mode,
            context_candidate_budget=args.context_candidate_budget,
            context_components=args.context_components,
            stagnation_limit_seconds=args.stagnation_seconds,
            minimum_runtime_before_stagnation_stop=args.minimum_stagnation_runtime,
            reheat_stagnation_iterations=args.reheat_stagnation_iterations,
            reheat_factor=args.reheat_factor,
            max_reheats=args.max_reheats,
        )
        stage2 = Q1ALNSConfig(
            iterations=args.stage_iterations,
            time_limit_seconds=10.0**9,
            min_destroy_routes=args.destroy_min_2,
            max_destroy_routes=args.destroy_max_2,
            max_service_nodes=2,
            repair_time_limit_seconds=args.repair_time_limit_2,
            seed=seed + 1,
            initial_temperature=args.temperature,
            cooling_rate=args.cooling_rate,
            reaction_factor=args.reaction_factor,
            segment_length=args.segment_length,
            related_destroy_mode=args.related_destroy_mode,
            frozen_consensus=args.frozen_consensus,
            context_repair_mode=args.context_repair_mode,
            context_candidate_budget=args.context_candidate_budget,
            context_components=args.context_components,
            stagnation_limit_seconds=args.stagnation_seconds,
            minimum_runtime_before_stagnation_stop=args.minimum_stagnation_runtime,
            reheat_stagnation_iterations=args.reheat_stagnation_iterations,
            reheat_factor=args.reheat_factor,
            max_reheats=args.max_reheats,
        )
    else:
        stage1 = Q1ALNSConfig(
            iterations=10**6,
            time_limit_seconds=args.wall_budget * 0.5,
            min_destroy_routes=args.destroy_min_1,
            max_destroy_routes=args.destroy_max_1,
            max_service_nodes=2,
            repair_time_limit_seconds=args.repair_time_limit_1,
            seed=seed,
            initial_temperature=args.temperature,
            cooling_rate=args.cooling_rate,
            reaction_factor=args.reaction_factor,
            segment_length=args.segment_length,
            related_destroy_mode=args.related_destroy_mode,
            frozen_consensus=args.frozen_consensus,
            context_repair_mode=args.context_repair_mode,
            context_candidate_budget=args.context_candidate_budget,
            context_components=args.context_components,
            stagnation_limit_seconds=args.stagnation_seconds,
            minimum_runtime_before_stagnation_stop=args.minimum_stagnation_runtime,
            reheat_stagnation_iterations=args.reheat_stagnation_iterations,
            reheat_factor=args.reheat_factor,
            max_reheats=args.max_reheats,
        )
        stage2 = Q1ALNSConfig(
            iterations=10**6,
            time_limit_seconds=args.wall_budget * 0.5,
            min_destroy_routes=args.destroy_min_2,
            max_destroy_routes=args.destroy_max_2,
            max_service_nodes=2,
            repair_time_limit_seconds=args.repair_time_limit_2,
            seed=seed + 1,
            initial_temperature=args.temperature,
            cooling_rate=args.cooling_rate,
            reaction_factor=args.reaction_factor,
            segment_length=args.segment_length,
            related_destroy_mode=args.related_destroy_mode,
            frozen_consensus=args.frozen_consensus,
            context_repair_mode=args.context_repair_mode,
            context_candidate_budget=args.context_candidate_budget,
            context_components=args.context_components,
            stagnation_limit_seconds=args.stagnation_seconds,
            minimum_runtime_before_stagnation_stop=args.minimum_stagnation_runtime,
            reheat_stagnation_iterations=args.reheat_stagnation_iterations,
            reheat_factor=args.reheat_factor,
            max_reheats=args.max_reheats,
        )
    return stage1, stage2


def _run_seed(args: argparse.Namespace, data, initial, seed: int, run_dir: Path) -> dict:
    run_dir.mkdir(parents=True)
    cache = SolverCache(data)
    stage1, stage2 = _stage_configs(args, seed)
    _EVAL_CALLS["count"] = 0
    started = time.perf_counter()
    stage_results = []
    stage_input = initial
    for stage_config in (stage1, stage2):
        stage_result = improve_q1_alns(
            stage_input,
            data,
            SolverConfig(seed=stage_config.seed),
            stage_config,
            cache=cache,
        )
        stage_results.append(stage_result)
        stage_input = stage_result.solution
    solve_seconds = time.perf_counter() - started
    solution = stage_results[-1].solution

    convergence_rows: list[dict[str, object]] = []
    global_iteration = 0
    elapsed_offset = 0.0
    for stage_number, stage_result in enumerate(stage_results, start=1):
        for row in stage_result.convergence:
            global_iteration += 1
            convergence_rows.append(
                {
                    "stage": stage_number,
                    "global_iteration": global_iteration,
                    **row,
                    "elapsed_seconds": round(elapsed_offset + float(row["elapsed_seconds"]), 6),
                }
            )
        if stage_result.convergence:
            elapsed_offset = float(convergence_rows[-1]["elapsed_seconds"])
    operator_rows = []
    for stage_number, stage_result in enumerate(stage_results, start=1):
        operator_rows.extend({"stage": stage_number, **row} for row in stage_result.operator_stats)

    export_q1_solution(solution, run_dir / "q1-routes.csv", run_dir / "q1-assignments.csv")
    validation = validate_solution(
        "q1",
        run_dir / "q1-routes.csv",
        run_dir / "q1-assignments.csv",
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    validator_metrics = validation.metrics.to_dict() if validation.metrics else None
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
        and validator_metrics
        and validator_metrics["served_passengers"] == data.q1_passenger_count
        and metrics_match
    )

    initial_aircraft = float(initial.metrics.total_aircraft_time_minutes)
    best_aircraft = float(internal_metrics["total_aircraft_time_minutes"])
    first_below = next(
        (
            float(row["elapsed_seconds"])
            for row in convergence_rows
            if float(row["best_aircraft_time_minutes"]) < initial_aircraft
        ),
        None,
    )
    final_best_row = next(
        (
            row
            for row in convergence_rows
            if float(row["best_aircraft_time_minutes"]) == best_aircraft
        ),
        None,
    )
    time_to_final_best = float(final_best_row["elapsed_seconds"]) if final_best_row else 0.0
    accepted_total = sum(int(row["accepted"]) for row in convergence_rows)
    improved_total = sum(int(row["improved_current"]) for row in convergence_rows)
    global_best_total = sum(int(row["new_global_best"]) for row in convergence_rows)
    repair_successes = sum(bool(row["repaired_routes"] != "") for row in convergence_rows)
    repair_candidates_considered = sum(
        int(row["repair_candidates_considered"] or 0) for row in convergence_rows
    )
    repair_candidates_selected = sum(
        int(row["repair_candidates_selected"] or 0) for row in convergence_rows
    )
    repair_exact_candidate_builds = sum(
        int(row["repair_exact_candidate_builds"] or 0) for row in convergence_rows
    )

    def time_to_threshold(threshold: float) -> float | None:
        return next(
            (
                float(row["elapsed_seconds"])
                for row in convergence_rows
                if float(row["best_aircraft_time_minutes"]) <= threshold
            ),
            None,
        )

    write_csv(
        run_dir / "q1-convergence.csv",
        [
            "stage",
            "global_iteration",
            "iteration",
            "elapsed_seconds",
            "operator",
            "destroyed_routes",
            "destroyed_passengers",
            "removed_aircraft_time_minutes",
            "repair_variants",
            "repaired_routes",
            "repair_candidates_considered",
            "repair_candidates_selected",
            "repair_exact_candidate_builds",
            "accepted",
            "improved_current",
            "new_global_best",
            "current_aircraft_time_minutes",
            "best_aircraft_time_minutes",
            "current_passenger_time_minutes",
            "best_passenger_time_minutes",
            "current_flights",
            "best_flights",
            "temperature",
        ],
        convergence_rows,
    )
    write_csv(
        run_dir / "operator_stats.csv",
        ["stage", "operator", "weight", "calls", "accepted", "improved", "new_global_best",
         "feasible_repairs", "failed_repairs", "total_gain_minutes",
         "mean_gain_when_improving", "runtime_seconds", "mean_destroyed_routes"],
        operator_rows,
    )
    weight_rows: list[dict[str, object]] = []
    for stage_number, stage_result in enumerate(stage_results, start=1):
        weight_rows.extend(
            {"stage": stage_number, **row} for row in stage_result.weight_history
        )
    write_csv(
        run_dir / "weight_history.csv",
        ["stage", "iteration", "operator", "weight"],
        weight_rows,
    )
    write_json(run_dir / "validator.json", validation.to_dict())

    summary = {
        "experiment": args.experiment,
        "mode": args.mode,
        "seed": seed,
        "git_commit": _git_commit(),
        "initial_routes": str(args.initial_routes),
        "initial_aircraft_time_minutes": initial_aircraft,
        "best_aircraft_time_minutes": best_aircraft,
        "final_comparison_key": list(_comparison_key(validator_metrics)) if validator_metrics else None,
        "passenger_time_minutes": internal_metrics["total_passenger_travel_time_minutes"],
        "flights": internal_metrics["total_flights"],
        "fuel_kg": internal_metrics["total_fuel_consumption_kg"],
        "seat_utilization": internal_metrics["seat_utilization"],
        "served_passengers": internal_metrics["served_passengers"],
        "gate_pass": gate_pass,
        "metrics_match": metrics_match,
        "validator_valid": bool(validation.valid),
        "beat_initial": best_aircraft < initial_aircraft,
        "runtime_seconds": round(solve_seconds, 3),
        "iterations_completed": len(convergence_rows),
        "accepted_moves": accepted_total,
        "improving_moves": improved_total,
        "accepted_deteriorating_moves": accepted_total - improved_total,
        "new_global_best_count": global_best_total,
        "time_to_first_improvement_seconds": first_below,
        "time_to_final_best_seconds": round(time_to_final_best, 3),
        "time_to_15118_seconds": time_to_threshold(15118),
        "time_to_15052_seconds": time_to_threshold(15052),
        "evaluator_calls": _EVAL_CALLS["count"],
        "repair_successes": repair_successes,
        "repair_success_rate": round(
            repair_successes / max(1, len(convergence_rows)), 6
        ),
        "repair_candidates_considered": repair_candidates_considered,
        "repair_candidates_selected": repair_candidates_selected,
        "repair_exact_candidate_builds": repair_exact_candidate_builds,
        "candidate_selection_rate": round(
            repair_candidates_selected / max(1, repair_candidates_considered), 6
        ),
        "improvement_per_evaluator_call": round(
            (initial_aircraft - best_aircraft) / max(1, _EVAL_CALLS["count"]),
            9,
        ),
        "solver_cache_stats": cache.stats(),
        "variant_cache_size": solution.diagnostics.get("alns", {}).get("variant_cache_size"),
        "stage_configs": [
            {
                "iterations": config.iterations,
                "time_limit_seconds": config.time_limit_seconds,
                "min_destroy_routes": config.min_destroy_routes,
                "max_destroy_routes": config.max_destroy_routes,
                "repair_time_limit_seconds": config.repair_time_limit_seconds,
                "initial_temperature": config.initial_temperature,
                "cooling_rate": config.cooling_rate,
                "reaction_factor": config.reaction_factor,
                "segment_length": config.segment_length,
                "related_destroy_mode": config.related_destroy_mode,
                "consensus_source": (
                    str(args.consensus_path)
                    if config.related_destroy_mode == "distance_consensus"
                    else None
                ),
                "context_repair_mode": config.context_repair_mode,
                "context_candidate_budget": config.context_candidate_budget,
                "context_components": list(config.context_components),
                "stagnation_limit_seconds": config.stagnation_limit_seconds,
                "minimum_runtime_before_stagnation_stop": (
                    config.minimum_runtime_before_stagnation_stop
                ),
                "stop_reason": stage_results[
                    0 if config is stage1 else 1
                ].solution.diagnostics.get("alns", {}).get("stop_reason"),
                "seed": config.seed,
            }
            for config in (stage1, stage2)
        ],
        "operator_stats": operator_rows,
    }
    write_json(run_dir / "run_summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Q1 Standard ALNS multi-seed experiment runner")
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--mode", choices=("iteration", "wallclock"), required=True)
    parser.add_argument("--seeds", required=True, help="comma separated seed list, e.g. 0,1,2,3,4")
    parser.add_argument("--stage-iterations", type=int, default=60)
    parser.add_argument("--wall-budget", type=float, default=300.0)
    parser.add_argument("--repair-time-limit-1", type=float, default=3.0)
    parser.add_argument("--repair-time-limit-2", type=float, default=5.0)
    parser.add_argument("--destroy-min-1", type=int, default=2)
    parser.add_argument("--temperature", type=float, default=0.002)
    parser.add_argument("--cooling-rate", type=float, default=0.985)
    parser.add_argument("--reaction-factor", type=float, default=0.25)
    parser.add_argument("--segment-length", type=int, default=15)
    parser.add_argument(
        "--related-destroy-mode",
        choices=("legacy", "distance", "distance_consensus"),
        default="legacy",
    )
    parser.add_argument(
        "--consensus-path",
        type=Path,
        default=ROOT / "data" / "q1-relatedness-consensus.csv",
    )
    parser.add_argument(
        "--context-repair-mode",
        choices=("none", "ranked"),
        default="none",
    )
    parser.add_argument("--context-candidate-budget", type=int, default=0)
    parser.add_argument(
        "--context-components",
        default="geometry,capacity,ejection,airport,route_state",
    )
    parser.add_argument("--stagnation-seconds", type=float)
    parser.add_argument("--minimum-stagnation-runtime", type=float, default=0.0)
    parser.add_argument("--reheat-stagnation-iterations", type=int)
    parser.add_argument("--reheat-factor", type=float, default=2.0)
    parser.add_argument("--max-reheats", type=int, default=0)
    parser.add_argument("--destroy-max-1", type=int, default=3)
    parser.add_argument("--destroy-min-2", type=int, default=3)
    parser.add_argument("--destroy-max-2", type=int, default=4)
    parser.add_argument(
        "--initial-routes",
        type=Path,
        default=ROOT / "outputs" / "q1" / "best" / "q1-routes.csv",
    )
    parser.add_argument(
        "--initial-assignments",
        type=Path,
        default=ROOT / "outputs" / "q1" / "best" / "q1-assignments.csv",
    )
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "q1" / "alns")
    args = parser.parse_args()
    args.context_components = tuple(
        item.strip() for item in args.context_components.split(",") if item.strip()
    )

    seeds = [int(item) for item in args.seeds.split(",") if item.strip()]
    experiment_dir = args.output_root / args.experiment
    if experiment_dir.exists():
        raise FileExistsError(f"experiment directory already exists: {experiment_dir}")
    experiment_dir.mkdir(parents=True)

    data = load_problem_data()
    args.frozen_consensus = (
        FrozenConsensus.from_pair_csv(args.consensus_path, data.config.facilities)
        if args.related_destroy_mode == "distance_consensus"
        else None
    )
    initial = load_q1_solution(
        args.initial_routes, args.initial_assignments, data, method="q1_alns_initial"
    )
    initial_key = _comparison_key(initial.metrics.to_dict())
    summaries = []
    for seed in seeds:
        run_dir = experiment_dir / f"seed-{seed}"
        summary = _run_seed(args, data, initial, seed, run_dir)
        summaries.append(summary)
        print(
            f"seed={seed} best={summary['best_aircraft_time_minutes']:.0f} "
            f"gate={summary['gate_pass']} runtime={summary['runtime_seconds']:.1f}s "
            f"improving={summary['improving_moves']} global_best={summary['new_global_best_count']}",
            flush=True,
        )

    aircraft = [float(item["best_aircraft_time_minutes"]) for item in summaries]
    passenger = [float(item["passenger_time_minutes"]) for item in summaries]
    aggregate = {
        "experiment": args.experiment,
        "mode": args.mode,
        "created_utc": datetime.utcnow().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "seeds": seeds,
        "initial_comparison_key": list(initial_key),
        "initial_aircraft_time_minutes": float(initial.metrics.total_aircraft_time_minutes),
        "best_of_seeds_aircraft_time_minutes": min(aircraft),
        "median_aircraft_time_minutes": statistics.median(aircraft),
        "mean_aircraft_time_minutes": round(statistics.mean(aircraft), 3),
        "worst_aircraft_time_minutes": max(aircraft),
        "stdev_aircraft_time_minutes": round(statistics.stdev(aircraft), 3) if len(aircraft) > 1 else 0.0,
        "median_passenger_time_minutes": statistics.median(passenger),
        "beat_initial_count": sum(1 for item in summaries if item["beat_initial"]),
        "beat_initial_rate": round(
            sum(1 for item in summaries if item["beat_initial"]) / len(summaries), 3
        ),
        "gate_pass_all": all(item["gate_pass"] for item in summaries),
        "per_seed": [
            {
                "seed": item["seed"],
                "best_aircraft_time_minutes": item["best_aircraft_time_minutes"],
                "passenger_time_minutes": item["passenger_time_minutes"],
                "flights": item["flights"],
                "runtime_seconds": item["runtime_seconds"],
                "improving_moves": item["improving_moves"],
                "new_global_best_count": item["new_global_best_count"],
                "evaluator_calls": item["evaluator_calls"],
                "repair_candidates_considered": item["repair_candidates_considered"],
                "repair_candidates_selected": item["repair_candidates_selected"],
                "gate_pass": item["gate_pass"],
            }
            for item in summaries
        ],
    }
    write_json(experiment_dir / "aggregate_summary.json", aggregate)
    write_csv(
        experiment_dir / "per_seed_summary.csv",
        [
            "seed",
            "best_aircraft_time_minutes",
            "passenger_time_minutes",
            "flights",
            "fuel_kg",
            "seat_utilization",
            "runtime_seconds",
            "iterations_completed",
            "accepted_moves",
            "improving_moves",
            "accepted_deteriorating_moves",
            "new_global_best_count",
            "time_to_first_improvement_seconds",
            "time_to_final_best_seconds",
            "evaluator_calls",
            "repair_candidates_considered",
            "repair_candidates_selected",
            "repair_exact_candidate_builds",
            "candidate_selection_rate",
            "repair_success_rate",
            "improvement_per_evaluator_call",
            "gate_pass",
            "beat_initial",
        ],
        summaries,
    )
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    return 0 if aggregate["gate_pass_all"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
