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

from src.io_utils import sha256, write_json
from src.solver import (
    Q2LnsConfig,
    SolverCache,
    atomic_promote_q2_run,
    exact_q2_elite_recombination,
    export_q1_solution,
    load_problem_data,
    load_q2_solution,
    q2_solution_diversity,
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
        description="Q2 elite-difference destroy with exact local recombination"
    )
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "q2")
    parser.add_argument("--start-dir", type=Path, required=True)
    parser.add_argument("--partner-dir", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=8)
    parser.add_argument("--neighborhood-size", type=int, default=4)
    parser.add_argument("--candidate-sequence-budget", type=int, default=24)
    parser.add_argument("--max-sequence-length", type=int, default=5)
    parser.add_argument("--local-primary-time-limit", type=float, default=8.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q2-elite"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()
    data = load_problem_data()
    current = load_q2_solution(
        args.start_dir / "q2-routes.csv",
        args.start_dir / "q2-assignments.csv",
        data,
        method="q2_elite_start",
    )
    initial = current
    partner = load_q2_solution(
        args.partner_dir / "q2-routes.csv",
        args.partner_dir / "q2-assignments.csv",
        data,
        method="q2_elite_partner",
    )
    config = Q2LnsConfig(
        iterations=args.attempts,
        neighborhood_size=args.neighborhood_size,
        max_sequence_length=args.max_sequence_length,
        candidate_sequence_budget=args.candidate_sequence_budget,
        local_primary_seconds=args.local_primary_time_limit,
        local_secondary_seconds=0.0,
        seed=args.seed,
        candidate_policy="geometry",
        targeted_four_stop=True,
    )
    cache = SolverCache(data)
    logs: list[dict[str, object]] = []
    candidate_logs: list[dict[str, object]] = []
    for attempt in range(args.attempts):
        before = current.metrics
        repair = exact_q2_elite_recombination(
            current,
            partner,
            data,
            cache=cache,
            config=config,
            iteration=args.seed * args.attempts + attempt,
        )
        candidate = repair.solution
        accepted = bool(
            candidate is not None
            and candidate.metrics.comparison_key() < current.metrics.comparison_key()
        )
        primary_gain = (
            before.total_aircraft_time_minutes
            - candidate.metrics.total_aircraft_time_minutes
            if candidate is not None
            else 0
        )
        if accepted:
            current = candidate
        logs.append(
            {
                "attempt": attempt,
                "accepted": accepted,
                "primary_gain": primary_gain if accepted else 0,
                "current_objective": current.metrics.total_aircraft_time_minutes,
                **{
                    key: value
                    for key, value in repair.diagnostics.items()
                    if key != "candidate_log"
                },
            }
        )
        for row in repair.diagnostics.get("candidate_log", []):
            candidate_logs.append(
                {
                    "run_id": run_id,
                    "seed": args.seed,
                    "iteration": attempt,
                    "destroy_operator": "elite_difference",
                    "destroy_size": args.neighborhood_size,
                    "source_routes": repair.diagnostics.get("elite_neighborhood", []),
                    **row,
                    "repair_feasible": candidate is not None,
                    "repair_accepted": accepted,
                    "primary_gain": primary_gain if accepted else 0,
                    "secondary_gain": 0,
                    "new_global_best": accepted,
                }
            )

    routes_path = run_dir / "q2-routes.csv"
    assignments_path = run_dir / "q2-assignments.csv"
    export_q1_solution(current, routes_path, assignments_path)
    validation = validate_solution(
        "q2",
        routes_path,
        assignments_path,
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    write_json(run_dir / "q2-validator.json", validation.to_dict())
    validator_metrics = validation.metrics.to_dict() if validation.metrics else None
    internal_metrics = current.metrics.to_dict()
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
            "partner_metrics": partner.metrics.to_dict(),
            "internal_metrics": internal_metrics,
            "validator_metrics": validator_metrics,
            "comparison_key": list(_comparison_key(validator_metrics))
            if validator_metrics
            else None,
        },
    )
    with (run_dir / "search-log.jsonl").open("w", encoding="utf-8") as stream:
        for row in logs:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    with (run_dir / "candidate-log.jsonl").open("w", encoding="utf-8") as stream:
        for row in candidate_logs:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    elapsed = time.perf_counter() - started
    write_json(
        run_dir / "run_config.json",
        {
            "run_id": run_id,
            "method": "q2_elite_difference_exact_recombination",
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
            },
            "partner": {
                "directory": str(args.partner_dir.resolve()),
                "routes_sha256": sha256(args.partner_dir / "q2-routes.csv"),
                "assignments_sha256": sha256(args.partner_dir / "q2-assignments.csv"),
            },
            "elite_diversity": q2_solution_diversity(initial, partner),
            "config": {
                "attempts": args.attempts,
                "neighborhood_size": args.neighborhood_size,
                "candidate_sequence_budget": args.candidate_sequence_budget,
                "max_sequence_length": args.max_sequence_length,
                "local_primary_seconds": args.local_primary_time_limit,
                "seed": args.seed,
            },
            "cache": cache.stats(),
            "accepted_recombinations": sum(int(row["accepted"]) for row in logs),
            "candidate_rows": len(candidate_logs),
            "elapsed_seconds": round(elapsed, 6),
        },
    )
    if not gate_pass:
        print(f"Q2 ELITE GATE FAIL: {run_dir}", file=sys.stderr)
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
        "Q2 ELITE PASS: "
        f"time={validator_metrics['total_aircraft_time_minutes']} min, "
        f"accepted={sum(int(row['accepted']) for row in logs)}, "
        f"promoted={promoted}, elapsed={elapsed:.3f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
