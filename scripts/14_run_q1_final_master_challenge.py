from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import highspy

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import sha256, write_json
from src.solver import (
    Q1MasterConfig,
    audit_master_symmetry,
    build_frozen_incumbent_start,
    collect_elite_route_pool,
    export_q1_solution,
    load_problem_data,
    load_q1_solution,
    materialize_pattern_start,
    solve_highs_pattern_master,
)
from src.validation import validate_solution


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _result_payload(result) -> dict[str, object]:
    return {
        "outcome": result.outcome,
        "model_status": result.model_status,
        "run_status": result.run_status,
        "objective": result.objective,
        "dual_bound": result.dual_bound,
        "mip_gap": result.mip_gap,
        "node_count": result.node_count,
        "elapsed_seconds": result.elapsed_seconds,
        "primary_upper_bound_minutes": result.primary_upper_bound_minutes,
        "mip_start_backend_status": result.mip_start_backend_status,
        "mip_start_feasible_for_model": result.mip_start_feasible_for_model,
        "mip_start_maximum_row_violation": result.mip_start_maximum_row_violation,
        "stopped_for_stall": result.stopped_for_stall,
        "progress": list(result.progress),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Q1 strict 14,729 master challenge")
    parser.add_argument("--run-id")
    parser.add_argument("--strict-upper-bound", type=int, default=14729)
    parser.add_argument(
        "--stall-seconds",
        type=float,
        default=900.0,
        help="Interrupt only after this much time without node/bound/incumbent progress",
    )
    parser.add_argument(
        "--time-limit",
        type=float,
        help="Optional emergency wall limit; omitted by default",
    )
    parser.add_argument("--maximum-source-objective", type=int, default=15371)
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S-highspy")
    run_dir = ROOT / "outputs" / "q1" / "exact" / "final-master-challenge" / run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    data = load_problem_data()
    pool = collect_elite_route_pool(
        data,
        ROOT / "outputs" / "q1",
        maximum_objective=args.maximum_source_objective,
    )
    frozen = load_q1_solution(
        ROOT / "outputs" / "q1" / "final" / "q1-routes.csv",
        ROOT / "outputs" / "q1" / "final" / "q1-assignments.csv",
        data,
        method="FROZEN_Q1_14730_CONTROL",
    )
    config = Q1MasterConfig(mip_relative_gap=0.0)
    arrays, mip_start = build_frozen_incumbent_start(data, pool, frozen, config)
    if mip_start.missing_patterns or mip_start.maximum_equality_residual > 1.0e-7:
        raise RuntimeError(f"Frozen MIP-start mapping failed: {mip_start}")

    reproduction = materialize_pattern_start(data, arrays, mip_start)
    reproduction_dir = run_dir / "reproduction"
    reproduction_dir.mkdir()
    routes_path = reproduction_dir / "q1-routes.csv"
    assignments_path = reproduction_dir / "q1-assignments.csv"
    export_q1_solution(reproduction, routes_path, assignments_path)
    validation = validate_solution(
        "q1", routes_path, assignments_path, data_dir=ROOT / "data" / "raw"
    )
    metrics = validation.metrics.to_dict() if validation.metrics else None
    reproduction_pass = bool(
        validation.valid
        and metrics
        and metrics["total_aircraft_time_minutes"] == 14730
        and metrics["total_passenger_travel_time_minutes"] == 121363
        and metrics["total_flights"] == 89
        and metrics["served_passengers"] == 1600
    )
    if not reproduction_pass:
        raise RuntimeError("14,730 Master reproduction gate failed")
    write_json(reproduction_dir / "validator.json", validation.to_dict())

    backend_acceptance = solve_highs_pattern_master(
        data,
        pool,
        config=config,
        mip_start=mip_start,
        mip_max_nodes=0,
        log_path=run_dir / "mip-start-acceptance.log",
    )
    accepted = bool(
        backend_acceptance.mip_start_backend_status == "HighsStatus.kOk"
        and backend_acceptance.mip_start_feasible_for_model
        and backend_acceptance.objective == 14730
    )
    if not accepted:
        raise RuntimeError("HiGHS did not confirm the complete 14,730 MIP start")

    strict = solve_highs_pattern_master(
        data,
        pool,
        config=config,
        mip_start=mip_start,
        primary_upper_bound_minutes=args.strict_upper_bound,
        time_limit_seconds=args.time_limit,
        stall_limit_seconds=args.stall_seconds,
        log_path=run_dir / "strict-14729.log",
        output_flag=True,
    )
    strict_validation = None
    if strict.solution is not None:
        candidate_dir = run_dir / "candidate"
        candidate_dir.mkdir()
        candidate_routes = candidate_dir / "q1-routes.csv"
        candidate_assignments = candidate_dir / "q1-assignments.csv"
        export_q1_solution(strict.solution, candidate_routes, candidate_assignments)
        strict_validation = validate_solution(
            "q1",
            candidate_routes,
            candidate_assignments,
            data_dir=ROOT / "data" / "raw",
        )
        write_json(candidate_dir / "validator.json", strict_validation.to_dict())
        if not strict_validation.valid:
            raise RuntimeError("Strict Master candidate failed independent validation")

    symmetry = audit_master_symmetry(data, pool, config)
    write_json(run_dir / "symmetry-audit.json", symmetry)
    write_json(
        run_dir / "challenge.json",
        {
            "scope": "RESTRICTED allocated-route-pattern master only",
            "global_optimality_claim": False,
            "pool": {
                "sources": len(pool.sources),
                "physical_routes": len(pool.routes),
                "allocated_pattern_columns": len(arrays.columns),
                "demand_rows": arrays.equality.shape[0],
                "matrix_nonzeros": arrays.equality.nnz,
            },
            "frozen_mip_start": {
                "selected_columns": mip_start.selected_columns,
                "selected_flights": mip_start.selected_flights,
                "primary_objective": mip_start.primary_objective,
                "passenger_objective": mip_start.passenger_objective,
                "flights_objective": mip_start.flights_objective,
                "fuel_objective_kg": mip_start.fuel_objective_kg,
                "maximum_equality_residual": mip_start.maximum_equality_residual,
                "missing_patterns": list(mip_start.missing_patterns),
                "backend_accepted": accepted,
            },
            "reproduction": {
                "gate_pass": reproduction_pass,
                "validator": validation.to_dict(),
                "routes_sha256": sha256(routes_path),
                "assignments_sha256": sha256(assignments_path),
            },
            "backend_acceptance": _result_payload(backend_acceptance),
            "strict_challenge": _result_payload(strict),
            "strict_candidate_validator": (
                strict_validation.to_dict() if strict_validation else None
            ),
            "important_note": (
                "The 14,730 vector is a feasible accepted MIP start for the base master, "
                "but necessarily violates the added <=14,729 hard row by one minute. "
                "HiGHS receives it as a repair hint in the strict model; it is not a "
                "feasible strict incumbent."
            ),
            "runtime": {
                "python": platform.python_version(),
                "highspy": highspy.Highs().version(),
                "git_commit": _git("rev-parse", "HEAD"),
                "git_branch": _git("branch", "--show-current"),
                "git_dirty": bool(_git("status", "--porcelain")),
                "stall_seconds": args.stall_seconds,
                "emergency_time_limit": args.time_limit,
            },
        },
    )
    print(
        f"Q1 MASTER CHALLENGE {strict.outcome}: status={strict.model_status}, "
        f"objective={strict.objective}, bound={strict.dual_bound}, "
        f"nodes={strict.node_count}, elapsed={strict.elapsed_seconds}s, run={run_dir}"
    )
    return 0 if strict.outcome != "UNKNOWN" else 3


if __name__ == "__main__":
    raise SystemExit(main())
