from __future__ import annotations

import argparse
import subprocess
import sys
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
    load_problem_data,
    solve_fullspace_rmp_lp,
)


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run all nine exact Q1 pricing oracles")
    parser.add_argument("--run-id")
    parser.add_argument("--time-limit", type=float)
    parser.add_argument("--output-flag", action="store_true")
    args = parser.parse_args()
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S-pricing")
    run_dir = ROOT / "outputs" / "q1" / "exact" / "pricing-tests" / run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    data = load_problem_data()
    pool = collect_elite_route_pool(data, ROOT / "outputs" / "q1")
    rmp = solve_fullspace_rmp_lp(data, pool)
    write_json(
        run_dir / "initial-rmp.json",
        {
            "objective": rmp.objective,
            "demand_duals": {
                f"{origin}->{destination}": value
                for (origin, destination), value in rmp.demand_duals.items()
            },
            "minimum_retained_column_reduced_cost": float(rmp.reduced_costs.min()),
            "artificial_fractional_column_caps": False,
            "scope": "initial restricted master; not yet a global lower bound",
        },
    )

    results = []
    for base in data.config.airports:
        for aircraft_type in data.config.aircraft_types:
            result = exact_pricing(
                data,
                rmp.demand_duals,
                base,
                aircraft_type,
                time_limit_seconds=args.time_limit,
                output_flag=args.output_flag,
            )
            payload = asdict(result)
            write_json(run_dir / f"pricing-{base}-{aircraft_type}.json", payload)
            results.append(result)
            print(
                f"{base} x {aircraft_type}: status={result.status}, "
                f"rc={result.reduced_cost}, lb={result.dual_bound}, "
                f"nodes={result.node_count}, elapsed={result.elapsed_seconds}s",
                flush=True,
            )

    all_optimal = all(result.proven_optimal for result in results)
    no_negative = all(result.certified_no_negative_column for result in results)
    negative = [result for result in results if result.negative_column_found]
    status = (
        "ALL_NINE_NO_NEGATIVE_CERTIFIED"
        if no_negative
        else "NEGATIVE_COLUMNS_FOUND"
        if negative
        else "EXACT_PRICING_INCOMPLETE"
    )
    write_json(
        run_dir / "summary.json",
        {
            "status": status,
            "pricing_tolerance": PRICING_TOL,
            "all_nine_globally_optimal": all_optimal,
            "all_nine_no_negative_certified": no_negative,
            "negative_subproblems": [
                f"{result.base_airport}x{result.aircraft_type}" for result in negative
            ],
            "minimum_reduced_cost": min(
                result.reduced_cost
                for result in results
                if result.reduced_cost is not None
            ),
            "initial_rmp_objective": rmp.objective,
            "source_sha": _git("rev-parse", "HEAD"),
            "source_dirty": bool(_git("status", "--porcelain")),
            "results": [asdict(result) for result in results],
        },
    )
    print(f"Q1 EXACT PRICING {status}: run={run_dir}")
    return 0 if all_optimal or no_negative else 3


if __name__ == "__main__":
    raise SystemExit(main())
