from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_json
from src.solver import (
    ExactRouteColumn,
    choose_fractional_arc_branch,
    collect_elite_route_pool,
    initial_exact_columns,
    load_problem_data,
    solve_exact_column_rmp_lp,
)
from src.solver.models import RouteStop


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _load_generated(path: Path) -> list[ExactRouteColumn]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    columns = []
    for raw in payload["generated_columns"]:
        columns.append(
            ExactRouteColumn(
                column_id=raw["column_id"],
                base_airport=raw["base_airport"],
                aircraft_type=raw["aircraft_type"],
                stops=tuple(RouteStop(**stop) for stop in raw["stops"]),
                allocation_pattern=tuple(tuple(item) for item in raw["allocation_pattern"]),
                duration_minutes=int(raw["duration_minutes"]),
                source=raw["source"],
            )
        )
    return columns


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize rigorous Q1 Branch-and-Price root")
    parser.add_argument(
        "--cg-checkpoint",
        type=Path,
        default=ROOT / "outputs" / "q1" / "exact" / "column-generation"
        / "20260816-fullspace-cg" / "checkpoint.json",
    )
    parser.add_argument("--run-id", default="20260816-root-initialization")
    args = parser.parse_args()
    run_dir = ROOT / "outputs" / "q1" / "exact" / "branch-and-price" / args.run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    data = load_problem_data()
    pool = collect_elite_route_pool(data, ROOT / "outputs" / "q1")
    columns = list(initial_exact_columns(data, pool))
    columns.extend(_load_generated(args.cg_checkpoint))
    root = solve_exact_column_rmp_lp(data, columns)
    branch = choose_fractional_arc_branch(columns, root.selected_values)
    if branch is None:
        raise RuntimeError("Root LP has no fractional aggregate arc for branching")
    arc, value = branch
    value = float(value)
    lower = int(math.floor(value))
    upper = int(math.ceil(value))
    fractional_variables = int(sum(
        abs(value - round(value)) > 1.0e-7 for value in root.selected_values
    ))
    children = [
        {
            "node_id": "N1-L",
            "parent": "N0",
            "constraint": {
                "feature": "directed_arc_usage",
                "arc": list(arc),
                "sense": "<=",
                "rhs": lower,
            },
            "status": "OPEN_UNPRICED",
            "inherited_valid_lower_bound": root.objective,
        },
        {
            "node_id": "N1-R",
            "parent": "N0",
            "constraint": {
                "feature": "directed_arc_usage",
                "arc": list(arc),
                "sense": ">=",
                "rhs": upper,
            },
            "status": "OPEN_UNPRICED",
            "inherited_valid_lower_bound": root.objective,
        },
    ]
    checkpoint = {
        "status": "BRANCH_AND_PRICE_INCOMPLETE",
        "incumbent_ub": 14730,
        "global_lb": root.objective,
        "rigorous_gap_over_ub": (14730 - root.objective) / 14730,
        "processed_nodes": 1,
        "open_nodes": 2,
        "root": {
            "node_id": "N0",
            "status": "FULLY_PRICED_FRACTIONAL",
            "lp_bound": root.objective,
            "column_count": len(columns),
            "fractional_route_variables": fractional_variables,
            "selected_branch_arc": list(arc),
            "selected_branch_value": value,
        },
        "open_node_queue": children,
        "branching_correctness": (
            "Every legal column has a well-defined integer coefficient equal to the "
            "number of traversals of the directed physical arc. The disjunction "
            "usage<=floor(v) or usage>=ceil(v) covers every integer solution and "
            "excludes the current fractional root solution. Child LPs still require "
            "node-specific exact pricing before their bounds can be strengthened or "
            "they can be fathomed."
        ),
        "resume_requires": [
            "add child arc row to RMP",
            "include its dual coefficient in every base/type pricing objective",
            "run exact pricing to no-negative-column at each child",
            "checkpoint descendants and global best-bound queue",
        ],
        "source_sha": _git("rev-parse", "HEAD"),
        "source_dirty": bool(_git("status", "--porcelain")),
    }
    write_json(run_dir / "checkpoint.json", checkpoint)
    print(
        f"Q1 B&P ROOT INITIALIZED: LB={root.objective}, branch={arc}@{value}, "
        f"open=2, run={run_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
