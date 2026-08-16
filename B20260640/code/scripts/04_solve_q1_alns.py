from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import DEFAULT_CONFIG_PATH
from src.io_utils import sha256, write_csv, write_json
from src.solver import (
    Q1ALNSConfig,
    SolverCache,
    export_q1_solution,
    improve_q1_alns,
    improve_q1_savings,
    load_problem_data,
    load_q1_solution,
    solve_q1_baseline,
)
from src.solver.models import SolverConfig
from src.solver.relatedness import FrozenConsensus
from src.validation import validate_solution


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _git_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=False
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


def _promote_if_best(run_dir: Path, output_root: Path, metrics: dict[str, object]) -> bool:
    best_dir = output_root / "best"
    best_metrics_path = best_dir / "metrics.json"
    if best_metrics_path.exists():
        previous = json.loads(best_metrics_path.read_text(encoding="utf-8"))["validator_metrics"]
        if _comparison_key(previous) <= _comparison_key(metrics):
            return False
    best_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "q1-routes.csv",
        "q1-assignments.csv",
        "metrics.json",
        "validator.json",
        "run_config.json",
        "q1-convergence.csv",
        "operator_stats.csv",
    ):
        shutil.copy2(run_dir / name, best_dir / name)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="运行问题一 B0+B1+ALNS 求解器")
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "q1")
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--iterations", type=int, default=120)
    parser.add_argument("--time-limit", type=float, default=900.0)
    parser.add_argument("--repair-time-limit", type=float, default=4.0)
    parser.add_argument("--min-destroy", type=int, default=2)
    parser.add_argument("--max-destroy", type=int, default=3)
    parser.add_argument("--max-service-nodes", type=int, default=2)
    parser.add_argument("--max-long-service-orders", type=int, default=40)
    parser.add_argument("--max-neighbors", type=int, default=5)
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
        "--context-repair-mode", choices=("none", "ranked"), default="none"
    )
    parser.add_argument("--context-candidate-budget", type=int, default=0)
    parser.add_argument(
        "--context-components",
        default="geometry,capacity,ejection,airport,route_state",
    )
    parser.add_argument("--stagnation-seconds", type=float)
    parser.add_argument("--minimum-stagnation-runtime", type=float, default=0.0)
    parser.add_argument("--initial-routes", type=Path)
    parser.add_argument("--initial-assignments", type=Path)
    parser.add_argument(
        "--balanced",
        action="store_true",
        help="采用已验证的两阶段配置：2--3路线快速搜索，再进行3--4路线强化搜索",
    )
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + f"-alns-s{args.seed}"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"运行目录已存在：{run_dir}")
    run_dir.mkdir(parents=True)

    started = time.perf_counter()
    data = load_problem_data()
    cache = SolverCache(data)
    solver_config = SolverConfig(seed=args.seed)
    context_components = tuple(
        item.strip() for item in args.context_components.split(",") if item.strip()
    )
    frozen_consensus = (
        FrozenConsensus.from_pair_csv(args.consensus_path, data.config.facilities)
        if args.related_destroy_mode == "distance_consensus"
        else None
    )
    relatedness_options = {
        "related_destroy_mode": args.related_destroy_mode,
        "frozen_consensus": frozen_consensus,
        "context_repair_mode": args.context_repair_mode,
        "context_candidate_budget": args.context_candidate_budget,
        "context_components": context_components,
        "stagnation_limit_seconds": args.stagnation_seconds,
        "minimum_runtime_before_stagnation_stop": args.minimum_stagnation_runtime,
    }
    if bool(args.initial_routes) != bool(args.initial_assignments):
        raise ValueError("--initial-routes and --initial-assignments must be provided together")
    if args.initial_routes:
        savings = load_q1_solution(
            args.initial_routes,
            args.initial_assignments,
            data,
            method="q1_b1_imported",
        )
        baseline = None
    else:
        baseline = solve_q1_baseline(data, solver_config, cache=cache)
        savings = improve_q1_savings(
            baseline, data, solver_config, max_neighbors=args.max_neighbors, cache=cache
        )
    if args.balanced:
        stage_configs = (
            Q1ALNSConfig(
                iterations=60,
                time_limit_seconds=600.0,
                min_destroy_routes=2,
                max_destroy_routes=3,
                max_service_nodes=2,
                repair_time_limit_seconds=3.0,
                seed=args.seed,
                **relatedness_options,
            ),
            Q1ALNSConfig(
                iterations=60,
                time_limit_seconds=900.0,
                min_destroy_routes=3,
                max_destroy_routes=4,
                max_service_nodes=2,
                repair_time_limit_seconds=5.0,
                seed=args.seed + 1,
                **relatedness_options,
            ),
        )
    else:
        stage_configs = (
            Q1ALNSConfig(
                iterations=args.iterations,
                time_limit_seconds=args.time_limit,
                min_destroy_routes=args.min_destroy,
                max_destroy_routes=args.max_destroy,
                max_service_nodes=args.max_service_nodes,
                max_long_service_orders=args.max_long_service_orders,
                repair_time_limit_seconds=args.repair_time_limit,
                seed=args.seed,
                **relatedness_options,
            ),
        )
    stage_results = []
    stage_input = savings
    for stage_config in stage_configs:
        stage_solver_config = SolverConfig(seed=stage_config.seed)
        stage_result = improve_q1_alns(
            stage_input,
            data,
            stage_solver_config,
            stage_config,
            cache=cache,
        )
        stage_results.append(stage_result)
        stage_input = stage_result.solution
    solution = stage_results[-1].solution
    convergence_rows = []
    operator_rows = []
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
                    "elapsed_seconds": round(
                        elapsed_offset + float(row["elapsed_seconds"]), 6
                    ),
                }
            )
        if stage_result.convergence:
            elapsed_offset = float(convergence_rows[-1]["elapsed_seconds"])
        operator_rows.extend(
            {"stage": stage_number, **row} for row in stage_result.operator_stats
        )
    solve_seconds = time.perf_counter() - started

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
        ["stage", "operator", "weight", "calls", "accepted", "improved", "new_global_best"],
        operator_rows,
    )
    write_json(run_dir / "validator.json", validation.to_dict())
    write_json(
        run_dir / "metrics.json",
        {
            "gate_pass": gate_pass,
            "metrics_match": metrics_match,
            "baseline_metrics": baseline.metrics.to_dict() if baseline else None,
            "savings_metrics": savings.metrics.to_dict(),
            "stage_metrics": [result.solution.metrics.to_dict() for result in stage_results],
            "internal_metrics": internal_metrics,
            "validator_metrics": validator_metrics,
            "comparison_key": list(_comparison_key(validator_metrics)) if validator_metrics else None,
        },
    )
    write_json(
        run_dir / "run_config.json",
        {
            "run_id": run_id,
            "method": solution.method,
            "git_commit": _git_commit(),
            "git_dirty": _git_dirty(),
            "problem_config_sha256": sha256(DEFAULT_CONFIG_PATH),
            "seed": args.seed,
            "deterministic_given_seed": True,
            "solve_seconds": round(solve_seconds, 6),
            "elapsed_seconds": round(time.perf_counter() - started, 6),
            "max_neighbors": args.max_neighbors,
            "balanced_profile": args.balanced,
            "alns_config": {
                "iterations": args.iterations,
                "time_limit_seconds": args.time_limit,
                "repair_time_limit_seconds": args.repair_time_limit,
                "min_destroy_routes": args.min_destroy,
                "max_destroy_routes": args.max_destroy,
                "max_service_nodes": args.max_service_nodes,
                "max_long_service_orders": args.max_long_service_orders,
                "max_neighbors": args.max_neighbors,
                "initial_routes": str(args.initial_routes) if args.initial_routes else None,
                "initial_assignments": (
                    str(args.initial_assignments) if args.initial_assignments else None
                ),
                "related_destroy_mode": args.related_destroy_mode,
                "consensus_path": (
                    str(args.consensus_path)
                    if args.related_destroy_mode == "distance_consensus"
                    else None
                ),
                "context_repair_mode": args.context_repair_mode,
                "context_candidate_budget": args.context_candidate_budget,
                "context_components": list(context_components),
                "stagnation_seconds": args.stagnation_seconds,
                "minimum_stagnation_runtime": args.minimum_stagnation_runtime,
            },
            "alns_stages": [
                {
                    "iterations": config.iterations,
                    "time_limit_seconds": config.time_limit_seconds,
                    "repair_time_limit_seconds": config.repair_time_limit_seconds,
                    "min_destroy_routes": config.min_destroy_routes,
                    "max_destroy_routes": config.max_destroy_routes,
                    "max_service_nodes": config.max_service_nodes,
                    "related_destroy_mode": config.related_destroy_mode,
                    "context_repair_mode": config.context_repair_mode,
                    "context_candidate_budget": config.context_candidate_budget,
                    "context_components": list(config.context_components),
                    "stagnation_limit_seconds": config.stagnation_limit_seconds,
                    "minimum_runtime_before_stagnation_stop": (
                        config.minimum_runtime_before_stagnation_stop
                    ),
                    "seed": config.seed,
                }
                for config in stage_configs
            ],
            "diagnostics": solution.diagnostics,
        },
    )

    promoted = bool(
        args.promote
        and gate_pass
        and _promote_if_best(run_dir, args.output_root, validator_metrics)
    )
    print(f"运行目录：{run_dir}")
    print(f"Q1 阶段门：{'PASS' if gate_pass else 'FAIL'}")
    print(f"Validator：{'PASS' if validation.valid else 'FAIL'}")
    print(f"服务人数：{solution.metrics.served_passengers}/{data.q1_passenger_count}")
    print(f"B0 指标：{baseline.metrics.to_dict() if baseline else '使用外部B1初解'}")
    print(f"B1 指标：{savings.metrics.to_dict()}")
    print(f"ALNS 指标：{validator_metrics}")
    print(f"是否提升 best：{promoted}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
