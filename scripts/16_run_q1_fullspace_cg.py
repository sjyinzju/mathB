from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_json
from src.solver import (
    PRICING_TOL,
    collect_elite_route_pool,
    exact_pricing,
    initial_exact_columns,
    load_problem_data,
    pricing_result_to_column,
    solve_exact_column_rmp_lp,
)


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Q1 full-space exact column generation")
    parser.add_argument("--run-id")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--pricing-time-limit", type=float)
    args = parser.parse_args()
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S-fullspace-cg")
    run_dir = ROOT / "outputs" / "q1" / "exact" / "column-generation" / run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    data = load_problem_data()
    pool = collect_elite_route_pool(data, ROOT / "outputs" / "q1")
    columns = list(initial_exact_columns(data, pool))
    identities = {column.identity for column in columns}
    initial_count = len(columns)
    trajectory = []
    generated = []
    iteration = 0
    final_status = "EXACT_PRICING_INCOMPLETE"

    while True:
        if args.max_iterations is not None and iteration >= args.max_iterations:
            final_status = "ITERATION_LIMIT"
            break
        iteration_dir = run_dir / f"iteration-{iteration:03d}"
        iteration_dir.mkdir()
        rmp = solve_exact_column_rmp_lp(data, columns)
        write_json(
            iteration_dir / "rmp.json",
            {
                "iteration": iteration,
                "objective": rmp.objective,
                "column_count": len(columns),
                "minimum_retained_reduced_cost": float(rmp.reduced_costs.min()),
                "demand_duals": {
                    f"{origin}->{destination}": value
                    for (origin, destination), value in rmp.demand_duals.items()
                },
                "artificial_fractional_column_caps": False,
            },
        )

        tasks = [
            (base, aircraft_type)
            for base in data.config.airports
            for aircraft_type in data.config.aircraft_types
        ]
        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_map = {
                executor.submit(
                    exact_pricing,
                    data,
                    rmp.demand_duals,
                    base,
                    aircraft_type,
                    time_limit_seconds=args.pricing_time_limit,
                ): (base, aircraft_type)
                for base, aircraft_type in tasks
            }
            for future in as_completed(future_map):
                result = future.result()
                results.append(result)
                write_json(
                    iteration_dir
                    / f"pricing-{result.base_airport}-{result.aircraft_type}.json",
                    asdict(result),
                )
                print(
                    f"iter={iteration} {result.base_airport}x{result.aircraft_type} "
                    f"status={result.status} rc={result.reduced_cost} "
                    f"lb={result.dual_bound} elapsed={result.elapsed_seconds}s",
                    flush=True,
                )
        results.sort(key=lambda result: (result.base_airport, result.aircraft_type))
        exact_gate = all(result.proven_optimal for result in results)
        negative = [
            result for result in results
            if result.reduced_cost is not None
            and result.reduced_cost < -PRICING_TOL
        ]
        added = []
        for result in negative:
            column = pricing_result_to_column(
                result, source=f"exact_cg_iteration_{iteration}"
            )
            if column.identity in identities:
                continue
            identities.add(column.identity)
            columns.append(column)
            added.append(column)
            generated.append(column)
        row = {
            "iteration": iteration,
            "rmp_objective": rmp.objective,
            "columns_before": len(columns) - len(added),
            "negative_subproblems": len(negative),
            "columns_added": len(added),
            "minimum_pricing_reduced_cost": min(
                result.reduced_cost for result in results
                if result.reduced_cost is not None
            ),
            "all_pricing_optimal": exact_gate,
        }
        trajectory.append(row)
        write_json(iteration_dir / "summary.json", row)
        write_json(
            run_dir / "checkpoint.json",
            {
                "status": "RUNNING" if added else "TERMINATING",
                "completed_iteration": iteration,
                "initial_columns": initial_count,
                "current_columns": len(columns),
                "trajectory": trajectory,
                "generated_columns": [asdict(column) for column in generated],
                "source_sha": _git("rev-parse", "HEAD"),
                "source_dirty": bool(_git("status", "--porcelain")),
            },
        )
        if not added:
            if exact_gate and all(
                result.certified_no_negative_column for result in results
            ):
                final_status = "FULLSPACE_LP_CERTIFIED"
            elif negative:
                final_status = "DUPLICATE_NEGATIVE_COLUMN_ERROR"
            else:
                final_status = "EXACT_PRICING_INCOMPLETE"
            break
        iteration += 1

    final_rmp = solve_exact_column_rmp_lp(data, columns)
    write_json(
        run_dir / "summary.json",
        {
            "status": final_status,
            "initial_columns": initial_count,
            "final_columns": len(columns),
            "generated_columns": len(generated),
            "iterations_with_pricing": len(trajectory),
            "final_lp_objective": final_rmp.objective,
            "ceil_final_lp_objective": __import__("math").ceil(
                final_rmp.objective - PRICING_TOL
            ),
            "validated_ub": 14730,
            "pricing_tolerance": PRICING_TOL,
            "trajectory": trajectory,
            "source_sha": _git("rev-parse", "HEAD"),
            "source_dirty": bool(_git("status", "--porcelain")),
        },
    )
    print(
        f"Q1 FULLSPACE CG {final_status}: LP={final_rmp.objective}, "
        f"columns={len(columns)}, iterations={len(trajectory)}, run={run_dir}"
    )
    return 0 if final_status == "FULLSPACE_LP_CERTIFIED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
