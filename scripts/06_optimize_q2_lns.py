from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import scipy

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import sha256, write_csv, write_json
from src.solver import (
    DESTROY_OPERATORS,
    Q2LnsConfig,
    SolverCache,
    atomic_promote_q2_run,
    export_q1_solution,
    load_problem_data,
    load_q2_solution,
    solve_q2_lns,
)
from src.validation import validate_solution


def _git_value(*args: str) -> str | None:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _comparison_key(metrics: dict[str, object]) -> tuple[float, ...]:
    return (
        float(metrics["total_aircraft_time_minutes"]),
        float(metrics["total_passenger_travel_time_minutes"]),
        float(metrics["total_flights"]),
        float(metrics["total_fuel_consumption_kg"]),
        -float(metrics["seat_utilization"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Q2 strict-improvement LNS with exact local MILP repair"
    )
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "q2")
    parser.add_argument(
        "--start-dir", type=Path, default=ROOT / "outputs" / "q2" / "best"
    )
    parser.add_argument("--iterations", type=int, default=24)
    parser.add_argument("--neighborhood-size", type=int, default=3)
    parser.add_argument("--source-pool-size", type=int, default=24)
    parser.add_argument("--target-pool-size", type=int, default=8)
    parser.add_argument("--max-sequence-length", type=int, default=2)
    parser.add_argument("--candidate-sequence-budget", type=int, default=24)
    parser.add_argument("--local-primary-time-limit", type=float, default=4.0)
    parser.add_argument("--local-secondary-time-limit", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--operators",
        default=",".join(DESTROY_OPERATORS),
        help="Comma-separated destroy operators",
    )
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q2-lns"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()
    data = load_problem_data()
    initial = load_q2_solution(
        args.start_dir / "q2-routes.csv",
        args.start_dir / "q2-assignments.csv",
        data,
        method="q2_rmp_control",
    )
    operators = tuple(value.strip() for value in args.operators.split(",") if value.strip())
    config = Q2LnsConfig(
        iterations=args.iterations,
        neighborhood_size=args.neighborhood_size,
        source_pool_size=args.source_pool_size,
        target_pool_size=args.target_pool_size,
        max_sequence_length=args.max_sequence_length,
        candidate_sequence_budget=args.candidate_sequence_budget,
        local_primary_seconds=args.local_primary_time_limit,
        local_secondary_seconds=args.local_secondary_time_limit,
        seed=args.seed,
        operators=operators,
    )
    cache = SolverCache(data)
    result = solve_q2_lns(initial, data, config=config, cache=cache)
    solution = result.solution

    routes_path = run_dir / "q2-routes.csv"
    assignments_path = run_dir / "q2-assignments.csv"
    export_q1_solution(solution, routes_path, assignments_path)
    validation = validate_solution(
        "q2",
        routes_path,
        assignments_path,
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    write_json(run_dir / "q2-validator.json", validation.to_dict())
    validator_metrics = validation.metrics.to_dict() if validation.metrics else None
    internal_metrics = solution.metrics.to_dict()
    metrics_match = bool(
        validator_metrics
        and all(
            abs(float(validator_metrics[key]) - float(value)) <= 1.0e-6
            for key, value in internal_metrics.items()
        )
    )
    gate_pass = bool(
        validation.valid
        and validator_metrics
        and int(validator_metrics["served_passengers"]) == data.q2_passenger_count
        and metrics_match
    )
    write_json(
        run_dir / "metrics.json",
        {
            "gate_pass": gate_pass,
            "metrics_match": metrics_match,
            "passenger_count": data.q2_passenger_count,
            "initial_metrics": initial.metrics.to_dict(),
            "internal_metrics": internal_metrics,
            "validator_metrics": validator_metrics,
            "comparison_key": list(_comparison_key(validator_metrics))
            if validator_metrics
            else None,
        },
    )
    with (run_dir / "search-log.jsonl").open("w", encoding="utf-8") as stream:
        for row in result.iteration_log:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    operator_fields = (
        "operator",
        "uses",
        "repair_success",
        "accepted",
        "primary_improvement",
        "new_best",
        "primary_gain_minutes",
        "runtime_seconds",
        "mean_local_master_size",
        "max_local_master_size",
    )
    write_csv(run_dir / "operator-stats.csv", operator_fields, result.operator_stats)
    elapsed = time.perf_counter() - started
    write_json(
        run_dir / "run_config.json",
        {
            "run_id": run_id,
            "method": solution.method,
            "git_commit": _git_value("rev-parse", "HEAD"),
            "git_branch": _git_value("branch", "--show-current"),
            "git_dirty": bool(_git_value("status", "--porcelain")),
            "runtime": {
                "python": platform.python_version(),
                "scipy": scipy.__version__,
                "backend": "scipy.optimize.milp/HiGHS",
            },
            "source": {
                "directory": str(args.start_dir.resolve()),
                "routes_sha256": sha256(args.start_dir / "q2-routes.csv"),
                "assignments_sha256": sha256(args.start_dir / "q2-assignments.csv"),
                "metrics": initial.metrics.to_dict(),
            },
            "config": {
                "iterations": config.iterations,
                "neighborhood_size": config.neighborhood_size,
                "source_pool_size": config.source_pool_size,
                "target_pool_size": config.target_pool_size,
                "max_sequence_length": config.max_sequence_length,
                "candidate_sequence_budget": config.candidate_sequence_budget,
                "local_primary_seconds": config.local_primary_seconds,
                "local_secondary_seconds": config.local_secondary_seconds,
                "seed": config.seed,
                "operators": list(config.operators),
            },
            "bound_scope": "restricted_local_master",
            "scope_note": (
                "Each bound/gap in search-log.jsonl applies only to that destroyed "
                "neighborhood and finite local candidate pool."
            ),
            "cache": cache.stats(),
            "operator_stats": list(result.operator_stats),
            "lns_elapsed_seconds": round(result.elapsed_seconds, 6),
            "total_elapsed_seconds": round(elapsed, 6),
        },
    )
    if not gate_pass:
        print(f"Q2 LNS GATE FAIL: {run_dir}", file=sys.stderr)
        return 2

    promoted = False
    if args.promote:
        best_dir = args.output_root / "best"
        best_metrics = json.loads(
            (best_dir / "metrics.json").read_text(encoding="utf-8")
        )["validator_metrics"]
        if _comparison_key(validator_metrics) < _comparison_key(best_metrics):
            atomic_promote_q2_run(run_dir, best_dir)
            promoted = True
    print(
        "Q2 LNS PASS: "
        f"time={validator_metrics['total_aircraft_time_minutes']} min, "
        f"passenger={validator_metrics['total_passenger_travel_time_minutes']} min, "
        f"flights={validator_metrics['total_flights']}, "
        f"fuel={validator_metrics['total_fuel_consumption_kg']} kg, "
        f"accepted={sum(int(row['accepted']) for row in result.iteration_log)}, "
        f"promoted={promoted}, elapsed={elapsed:.3f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
