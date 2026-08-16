from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import sha256, write_json
from src.solver import (
    Q1MasterConfig,
    build_frozen_incumbent_start,
    collect_elite_route_pool,
    initial_exact_columns,
    load_problem_data,
    load_q1_solution,
)
from src.solver.exporter import export_q1_solution
from src.solver.q1_branch_price import (
    exact_column_start_from_pattern_start,
    load_generated_exact_columns,
    solve_exact_column_integer_master,
)
from src.validation import validate_solution


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _payload(result):
    raw = asdict(result)
    raw.pop("solution")
    raw.pop("solution_vector")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Solve the final 1,041-column Q1 integer master")
    parser.add_argument("--run-id", default="20260816-final-1041-master")
    parser.add_argument("--strict-upper-bound", type=int, default=14729)
    parser.add_argument("--normal-time-limit", type=float, default=300.0)
    parser.add_argument("--strict-time-limit", type=float, default=None)
    args = parser.parse_args()
    run_dir = ROOT / "outputs" / "q1" / "exact" / "integer-master" / args.run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    data = load_problem_data()
    pool = collect_elite_route_pool(data, ROOT / "outputs" / "q1")
    columns = list(initial_exact_columns(data, pool))
    columns.extend(load_generated_exact_columns(
        ROOT / "outputs" / "q1" / "exact" / "column-generation"
        / "20260816-fullspace-cg" / "checkpoint.json"
    ))
    frozen = load_q1_solution(
        ROOT / "outputs" / "q1" / "final" / "q1-routes.csv",
        ROOT / "outputs" / "q1" / "final" / "q1-assignments.csv",
        data,
    )
    _, pattern_start = build_frozen_incumbent_start(
        data, pool, frozen, Q1MasterConfig()
    )
    start = exact_column_start_from_pattern_start(pattern_start.values, len(columns))
    if len(columns) != 1041:
        raise RuntimeError(f"Expected 1,041 exact columns, found {len(columns)}")

    normal = solve_exact_column_integer_master(
        data,
        columns,
        mip_start_values=start,
        time_limit_seconds=args.normal_time_limit,
        log_path=run_dir / "normal.log",
        output_flag=True,
    )
    normal_validation = None
    if normal.solution is not None:
        candidate = run_dir / "normal-candidate"
        candidate.mkdir()
        routes = candidate / "q1-routes.csv"
        assignments = candidate / "q1-assignments.csv"
        export_q1_solution(normal.solution, routes, assignments)
        normal_validation = validate_solution(
            "q1", routes, assignments, data_dir=ROOT / "data" / "raw"
        )
        write_json(candidate / "validator.json", normal_validation.to_dict())

    strict = solve_exact_column_integer_master(
        data,
        columns,
        mip_start_values=start,
        primary_upper_bound_minutes=args.strict_upper_bound,
        submit_infeasible_mip_start=True,
        time_limit_seconds=args.strict_time_limit,
        log_path=run_dir / "strict-14729.log",
        output_flag=True,
    )
    strict_validation = None
    if strict.solution is not None:
        candidate = run_dir / "strict-candidate"
        candidate.mkdir()
        routes = candidate / "q1-routes.csv"
        assignments = candidate / "q1-assignments.csv"
        export_q1_solution(strict.solution, routes, assignments)
        strict_validation = validate_solution(
            "q1", routes, assignments, data_dir=ROOT / "data" / "raw"
        )
        write_json(candidate / "validator.json", strict_validation.to_dict())

    normal_routes = run_dir / "normal-candidate" / "q1-routes.csv"
    normal_assignments = run_dir / "normal-candidate" / "q1-assignments.csv"
    write_json(
        run_dir / "summary.json",
        {
            "scope": "final fully-priced root registry integer master (1,041 columns)",
            "global_optimality_claim": False,
            "column_count": len(columns),
            "initial_columns": len(columns) - 38,
            "exact_generated_columns": 38,
            "mip_start": {
                "objective": pattern_start.primary_objective,
                "maximum_equality_residual": pattern_start.maximum_equality_residual,
                "missing_patterns": list(pattern_start.missing_patterns),
                "selected_columns": pattern_start.selected_columns,
            },
            "normal_master": _payload(normal),
            "normal_validator": normal_validation.to_dict() if normal_validation else None,
            "normal_artifact_hashes": (
                {
                    "routes_sha256": sha256(normal_routes),
                    "assignments_sha256": sha256(normal_assignments),
                }
                if normal_routes.exists() else None
            ),
            "strict_challenge": _payload(strict),
            "strict_validator": strict_validation.to_dict() if strict_validation else None,
            "runtime": {
                "python": platform.python_version(),
                "git_commit": _git("rev-parse", "HEAD"),
                "git_branch": _git("branch", "--show-current"),
                "git_dirty": bool(_git("status", "--porcelain")),
                "normal_primal_check_time_limit_seconds": args.normal_time_limit,
                "strict_challenge_time_limit_seconds": args.strict_time_limit,
            },
        },
    )
    print(
        f"Q1 1041 MASTER normal={normal.outcome}/{normal.objective} "
        f"strict={strict.outcome}/{strict.objective} run={run_dir}"
    )
    return 0 if normal.outcome != "UNKNOWN" and strict.outcome != "UNKNOWN" else 3


if __name__ == "__main__":
    raise SystemExit(main())
