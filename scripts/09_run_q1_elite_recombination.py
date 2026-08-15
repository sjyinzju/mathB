from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import sha256, write_json
from src.solver import (
    Q1MasterConfig,
    collect_elite_route_pool,
    export_q1_solution,
    load_problem_data,
    load_q1_solution,
    route_identity,
    solve_restricted_lp,
    solve_route_pool_master,
)
from src.validation import validate_solution


def _git(*args: str) -> str | None:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Q1 two-parent elite exact allocated-route recombination"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--parent-a", type=Path, required=True)
    parser.add_argument("--parent-b", type=Path, required=True)
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "outputs" / "q1" / "final-or"
    )
    parser.add_argument("--primary-seconds", type=float, default=90.0)
    args = parser.parse_args()

    run_dir = args.output_root / args.run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()
    data = load_problem_data()
    parent_a = load_q1_solution(
        args.parent_a / "q1-routes.csv",
        args.parent_a / "q1-assignments.csv",
        data,
        method="q1_elite_parent_a",
    )
    parent_b = load_q1_solution(
        args.parent_b / "q1-routes.csv",
        args.parent_b / "q1-assignments.csv",
        data,
        method="q1_elite_parent_b",
    )
    a_keys = [route_identity(route) for route in parent_a.routes]
    b_keys = [route_identity(route) for route in parent_b.routes]
    a_set = set(a_keys)
    b_set = set(b_keys)
    comparison = {
        "parent_a_metrics": parent_a.metrics.to_dict(),
        "parent_b_metrics": parent_b.metrics.to_dict(),
        "common_physical_route_identities": len(a_set & b_set),
        "parent_a_unique_identities": len(a_set - b_set),
        "parent_b_unique_identities": len(b_set - a_set),
        "parent_a_difference_indices": [
            index for index, key in enumerate(a_keys) if key not in b_set
        ],
        "parent_b_difference_indices": [
            index for index, key in enumerate(b_keys) if key not in a_set
        ],
    }
    pool = collect_elite_route_pool(
        data,
        ROOT / "outputs" / "q1",
        source_directories=(args.parent_a, args.parent_b),
    )
    config = Q1MasterConfig(
        primary_time_limit_seconds=args.primary_seconds,
        secondary_time_limit_seconds=20.0,
        primary_upper_bound_minutes=min(
            parent_a.metrics.total_aircraft_time_minutes,
            parent_b.metrics.total_aircraft_time_minutes,
        ),
    )
    lp = solve_restricted_lp(data, pool, config)
    master = solve_route_pool_master(data, pool, config)
    routes_path = run_dir / "q1-routes.csv"
    assignments_path = run_dir / "q1-assignments.csv"
    export_q1_solution(master.solution, routes_path, assignments_path)
    validation = validate_solution(
        "q1",
        routes_path,
        assignments_path,
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    if not validation.valid:
        raise RuntimeError("Elite recombination child failed Validator")
    metrics = validation.metrics.to_dict()
    write_json(run_dir / "validator.json", validation.to_dict())
    write_json(
        run_dir / "metrics.json",
        {
            "gate_pass": True,
            "validator_metrics": metrics,
            "beat_parent_a": master.solution.metrics.comparison_key()
            < parent_a.metrics.comparison_key(),
            "beat_parent_b": master.solution.metrics.comparison_key()
            < parent_b.metrics.comparison_key(),
        },
    )
    write_json(
        run_dir / "elite-comparison.json",
        {
            **comparison,
            "unique_physical_routes": len(pool.routes),
            "allocated_pattern_columns": master.variable_count,
            "lp_objective": lp.objective,
            "master_objective": master.primary_objective,
            "master_dual_bound": master.primary_dual_bound,
            "master_mip_gap": master.primary_mip_gap,
            "selected_multiplicity": master.selected_multiplicity,
        },
    )
    write_json(
        run_dir / "run_config.json",
        {
            "run_id": args.run_id,
            "method": "A2xR3 exact allocated-route-pattern recombination",
            "git_commit": _git("rev-parse", "HEAD"),
            "git_branch": _git("branch", "--show-current"),
            "git_dirty": bool(_git("status", "--porcelain")),
            "parents": {
                "a": str(args.parent_a.resolve()),
                "b": str(args.parent_b.resolve()),
                "a_routes_sha256": sha256(args.parent_a / "q1-routes.csv"),
                "b_routes_sha256": sha256(args.parent_b / "q1-routes.csv"),
            },
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        },
    )
    print(
        "Q1 ELITE RECOMBINATION PASS: "
        f"A={parent_a.metrics.total_aircraft_time_minutes}, "
        f"B={parent_b.metrics.total_aircraft_time_minutes}, "
        f"child={metrics['total_aircraft_time_minutes']}, "
        f"common={comparison['common_physical_route_identities']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

