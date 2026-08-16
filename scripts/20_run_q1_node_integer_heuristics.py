from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import sha256, write_json
from src.solver import (
    ArcBranchRow,
    Q1MasterConfig,
    build_frozen_incumbent_start,
    collect_elite_route_pool,
    exact_column_start_from_pattern_start,
    initial_exact_columns,
    load_generated_exact_columns,
    load_problem_data,
    load_q1_solution,
    solve_exact_column_integer_master,
)
from src.solver.exporter import export_q1_solution
from src.validation import validate_solution


def _row(raw):
    return ArcBranchRow(tuple(raw["arc"]), raw["sense"], int(raw["rhs"]))


def _payload(result):
    raw = asdict(result)
    raw.pop("solution")
    raw.pop("solution_vector")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Run node integer-RMP primal heuristics")
    parser.add_argument("--run-id", default="20260816-node-integer-heuristics")
    parser.add_argument("--time-limit", type=float, default=60.0)
    parser.add_argument("--strict-improvement-ub", type=int, default=14729)
    args = parser.parse_args()
    source_dir = (
        ROOT / "outputs" / "q1" / "exact" / "branch-and-price"
        / "20260816-root-children"
    )
    run_dir = ROOT / "outputs" / "q1" / "exact" / "branch-and-price" / args.run_id
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
    columns.extend(load_generated_exact_columns(
        source_dir / "global-column-registry.json"
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

    outcomes = []
    for node_id in ("N1-L", "N1-R"):
        node_summary = json.loads(
            (source_dir / node_id / "summary.json").read_text(encoding="utf-8")
        )
        history = tuple(_row(raw) for raw in node_summary["branch_history"])
        result = solve_exact_column_integer_master(
            data,
            columns,
            mip_start_values=start,
            branch_history=history,
            primary_upper_bound_minutes=args.strict_improvement_ub,
            time_limit_seconds=args.time_limit,
            log_path=run_dir / f"{node_id}.log",
            output_flag=True,
        )
        validation = None
        hashes = None
        if result.solution is not None:
            candidate_dir = run_dir / node_id
            candidate_dir.mkdir()
            routes = candidate_dir / "q1-routes.csv"
            assignments = candidate_dir / "q1-assignments.csv"
            export_q1_solution(result.solution, routes, assignments)
            validation = validate_solution(
                "q1", routes, assignments, data_dir=ROOT / "data" / "raw"
            )
            write_json(candidate_dir / "validator.json", validation.to_dict())
            hashes = {
                "routes_sha256": sha256(routes),
                "assignments_sha256": sha256(assignments),
            }
        outcomes.append(
            {
                "node_id": node_id,
                "branch_history": [asdict(row) for row in history],
                "result": _payload(result),
                "validator": validation.to_dict() if validation else None,
                "artifact_hashes": hashes,
                "ub_updated": bool(
                    validation and validation.valid and result.objective < 14730
                ),
            }
        )
        print(
            f"{node_id} integer heuristic outcome={result.outcome} "
            f"objective={result.objective} start_feasible="
            f"{result.mip_start_feasible_for_model}",
            flush=True,
        )
    write_json(
        run_dir / "summary.json",
        {
            "scope": "primal heuristic only; never used as a lower bound",
            "column_count": len(columns),
            "strict_improvement_upper_bound": args.strict_improvement_ub,
            "time_limit_seconds_per_node": args.time_limit,
            "outcomes": outcomes,
            "global_ub_before": 14730,
            "global_ub_after": min(
                [14730]
                + [
                    item["result"]["objective"]
                    for item in outcomes
                    if item["ub_updated"]
                ]
            ),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
