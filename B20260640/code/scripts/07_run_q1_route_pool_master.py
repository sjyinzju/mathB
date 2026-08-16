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
    Q1MasterConfig,
    collect_elite_route_pool,
    export_q1_solution,
    load_problem_data,
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


def _master_payload(master) -> dict[str, object]:
    return {
        "primary_objective": master.primary_objective,
        "passenger_objective": master.passenger_objective,
        "flights_objective": master.flights_objective,
        "fuel_objective_kg": master.fuel_objective_kg,
        "primary_status": master.primary_status,
        "primary_proven_optimal": master.primary_proven_optimal,
        "primary_dual_bound": master.primary_dual_bound,
        "primary_mip_gap": master.primary_mip_gap,
        "stage_statuses": master.stage_statuses,
        "elapsed_seconds": master.elapsed_seconds,
        "variable_count": master.variable_count,
        "constraint_count": master.constraint_count,
        "matrix_nonzeros": master.compatible_allocations,
        "selected_multiplicity": master.selected_multiplicity,
        "allocation": master.allocation,
    }


def _lp_payload(lp) -> dict[str, object]:
    return {
        "objective": lp.objective,
        "status": lp.status,
        "success": lp.success,
        "elapsed_seconds": lp.elapsed_seconds,
        "demand_duals": lp.demand_duals,
        "selected_fractional_routes": list(lp.selected_fractional_routes),
        "scope_warning": (
            "RESTRICTED ROUTE-POOL LP ONLY; this is not a global Q1 lower bound."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Q1 elite allocated-route-pattern master and restricted LP"
    )
    parser.add_argument("--run-id")
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "outputs" / "q1" / "final-or"
    )
    parser.add_argument("--maximum-source-objective", type=int, default=15371)
    parser.add_argument("--primary-seconds", type=float, default=180.0)
    parser.add_argument("--secondary-seconds", type=float, default=30.0)
    parser.add_argument("--primary-upper-bound", type=int)
    parser.add_argument("--maximum-total-flights", type=int)
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-route-pool"
    run_dir = args.output_root / run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()
    data = load_problem_data()
    config = Q1MasterConfig(
        primary_time_limit_seconds=args.primary_seconds,
        secondary_time_limit_seconds=args.secondary_seconds,
        primary_upper_bound_minutes=args.primary_upper_bound,
        maximum_total_flights=args.maximum_total_flights,
    )
    control_config = Q1MasterConfig(
        primary_time_limit_seconds=min(args.primary_seconds, 30.0),
        secondary_time_limit_seconds=min(args.secondary_seconds, 10.0),
    )

    control_pool = collect_elite_route_pool(
        data,
        ROOT / "outputs" / "q1",
        maximum_objective=14770,
        exact_objective=14770,
    )
    control_lp = solve_restricted_lp(data, control_pool, control_config)
    control_master = solve_route_pool_master(data, control_pool, control_config)
    reproduction_pass = bool(
        control_master.primary_objective == 14770
        and control_master.solution.metrics.served_passengers == data.q1_passenger_count
    )
    if not reproduction_pass:
        raise RuntimeError("14,770 route-pool master reproduction gate failed")

    pool = collect_elite_route_pool(
        data,
        ROOT / "outputs" / "q1",
        maximum_objective=args.maximum_source_objective,
    )
    lp = solve_restricted_lp(data, pool, config)
    try:
        master = solve_route_pool_master(data, pool, config)
    except RuntimeError as error:
        write_json(
            run_dir / "no-incumbent.json",
            {
                "gate_pass": False,
                "outcome": "NO_MIP_INCUMBENT_WITHIN_LIMIT",
                "error": str(error),
                "lp": _lp_payload(lp),
                "pool_unique_routes": len(pool.routes),
                "pool_sources": len(pool.sources),
                "config": {
                    "maximum_source_objective": args.maximum_source_objective,
                    "primary_seconds": args.primary_seconds,
                    "secondary_seconds": args.secondary_seconds,
                    "primary_upper_bound": args.primary_upper_bound,
                    "maximum_total_flights": args.maximum_total_flights,
                },
                "scope_warning": (
                    "No incumbent is not an infeasibility proof and not a global Q1 bound."
                ),
            },
        )
        print(f"Q1 ROUTE-POOL MASTER NO INCUMBENT: {run_dir}", file=sys.stderr)
        return 3
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
    validator_metrics = validation.metrics.to_dict() if validation.metrics else None
    internal_metrics = master.solution.metrics.to_dict()
    metrics_match = bool(
        validator_metrics
        and all(
            abs(float(validator_metrics[key]) - float(value)) <= 1.0e-6
            for key, value in internal_metrics.items()
        )
    )
    gate_pass = bool(
        validation.valid
        and metrics_match
        and validator_metrics
        and int(validator_metrics["served_passengers"]) == data.q1_passenger_count
    )

    write_json(run_dir / "validator.json", validation.to_dict())
    write_json(
        run_dir / "metrics.json",
        {
            "gate_pass": gate_pass,
            "metrics_match": metrics_match,
            "internal_metrics": internal_metrics,
            "validator_metrics": validator_metrics,
            "comparison_key": [
                validator_metrics["total_aircraft_time_minutes"],
                validator_metrics["total_passenger_travel_time_minutes"],
                validator_metrics["total_flights"],
                validator_metrics["total_fuel_consumption_kg"],
                -validator_metrics["seat_utilization"],
            ]
            if validator_metrics
            else None,
        },
    )
    write_json(
        run_dir / "master.json",
        {
            "formulation": "exact allocated-route-pattern set partitioning",
            "control_reproduction_pass": reproduction_pass,
            "control": _master_payload(control_master),
            "expanded": _master_payload(master),
        },
    )
    write_json(
        run_dir / "lp-diagnostics.json",
        {
            "control": _lp_payload(control_lp),
            "expanded": _lp_payload(lp),
            "restricted_pool_gap": (
                master.primary_objective - lp.objective
            )
            / lp.objective,
        },
    )
    write_json(run_dir / "route-pool.json", pool.metadata())
    write_json(
        run_dir / "run_config.json",
        {
            "run_id": run_id,
            "method": "q1_exact_allocated_route_pattern_master",
            "git_commit": _git("rev-parse", "HEAD"),
            "git_branch": _git("branch", "--show-current"),
            "git_dirty": bool(_git("status", "--porcelain")),
            "runtime": {
                "python": platform.python_version(),
                "scipy": scipy.__version__,
                "backend": "scipy.optimize.milp/linprog (HiGHS)",
            },
            "config": {
                "maximum_source_objective": args.maximum_source_objective,
                "primary_seconds": args.primary_seconds,
                "secondary_seconds": args.secondary_seconds,
                "primary_upper_bound": args.primary_upper_bound,
                "maximum_total_flights": args.maximum_total_flights,
            },
            "elapsed_seconds": round(time.perf_counter() - started, 6),
            "artifacts": {
                "routes_sha256": sha256(routes_path),
                "assignments_sha256": sha256(assignments_path),
            },
        },
    )
    if not gate_pass:
        print(f"Q1 ROUTE-POOL MASTER GATE FAIL: {run_dir}", file=sys.stderr)
        return 2
    print(
        "Q1 ROUTE-POOL MASTER PASS: "
        f"time={validator_metrics['total_aircraft_time_minutes']} min, "
        f"flights={validator_metrics['total_flights']}, "
        f"lp={lp.objective:.6f}, pool={len(pool.routes)}, "
        f"sources={len(pool.sources)}, run={run_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
