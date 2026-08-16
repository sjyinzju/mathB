from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_json
from src.solver import (
    ArcBranchRow,
    BranchNode,
    collect_elite_route_pool,
    initial_exact_columns,
    load_generated_exact_columns,
    load_problem_data,
    solve_fully_priced_node,
)


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _row(raw: dict[str, object]) -> ArcBranchRow:
    return ArcBranchRow(
        tuple(raw["arc"]), str(raw["sense"]), int(raw["rhs"])
    )


def _write_node(run_dir: Path, result) -> dict[str, object]:
    node_dir = run_dir / result.node.node_id
    node_dir.mkdir()
    for record in result.iterations:
        iteration_dir = node_dir / f"iteration-{record.iteration:03d}"
        iteration_dir.mkdir()
        write_json(
            iteration_dir / "rmp.json",
            {
                "status": record.rmp.status,
                "proven_optimal": record.rmp.proven_optimal,
                "proven_infeasible": record.rmp.proven_infeasible,
                "objective": record.rmp.objective,
                "elapsed_seconds": record.rmp.elapsed_seconds,
                "demand_duals": {
                    f"{o}->{d}": value
                    for (o, d), value in record.rmp.demand_duals.items()
                },
                "branch_duals": [
                    {"row": asdict(row), "canonical_dual": dual}
                    for row, dual in record.rmp.branch_duals.items()
                ],
            },
        )
        for pricing in record.pricing_results:
            write_json(
                iteration_dir
                / f"pricing-{pricing.base_airport}-{pricing.aircraft_type}.json",
                asdict(pricing),
            )
        write_json(
            iteration_dir / "summary.json",
            {
                "iteration": record.iteration,
                "phase": record.phase,
                "rmp_objective": record.rmp.objective,
                "added_column_ids": list(record.added_column_ids),
                "minimum_reduced_cost": min(
                    pricing.reduced_cost
                    for pricing in record.pricing_results
                    if pricing.reduced_cost is not None
                ),
                "all_pricing_optimal": all(
                    pricing.proven_optimal for pricing in record.pricing_results
                ),
                "elapsed_seconds": record.elapsed_seconds,
            },
        )
    payload = {
        "node_id": result.node.node_id,
        "parent_id": result.node.parent_id,
        "depth": result.node.depth,
        "branch_history": [asdict(row) for row in result.node.branch_history],
        "status": result.status,
        "inherited_lb": result.node.inherited_lb,
        "initial_rmp_objective": result.initial_rmp_objective,
        "fully_priced_lb": result.fully_priced_lb,
        "effective_rigorous_lb": result.effective_rigorous_lb,
        "columns_before": result.columns_before,
        "columns_after": result.columns_after,
        "generated_columns": len(result.generated_columns),
        "cg_iterations": len(result.iterations),
        "pricing_calls": result.pricing_calls,
        "fractional_route_variables": result.fractional_route_variables,
        "selected_branch": (
            {"arc": list(result.selected_branch[0]), "value": result.selected_branch[1]}
            if result.selected_branch else None
        ),
        "rmp_seconds": result.rmp_seconds,
        "pricing_seconds": result.pricing_seconds,
        "elapsed_seconds": result.elapsed_seconds,
        "final_no_negative_column_gate": result.status == "FULLY_PRICED",
    }
    write_json(node_dir / "summary.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Fully price both Q1 root children")
    parser.add_argument("--run-id", default="20260816-root-children")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--pricing-time-limit", type=float, default=None)
    args = parser.parse_args()
    run_dir = ROOT / "outputs" / "q1" / "exact" / "branch-and-price" / args.run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    root_checkpoint = json.loads((
        ROOT / "outputs" / "q1" / "exact" / "branch-and-price"
        / "20260816-root-initialization" / "checkpoint.json"
    ).read_text(encoding="utf-8"))
    data = load_problem_data()
    pool = collect_elite_route_pool(data, ROOT / "outputs" / "q1")
    registry = list(initial_exact_columns(data, pool))
    registry.extend(load_generated_exact_columns(
        ROOT / "outputs" / "q1" / "exact" / "column-generation"
        / "20260816-fullspace-cg" / "checkpoint.json"
    ))
    initial_registry_count = len(registry)
    node_payloads = []
    for raw_node in root_checkpoint["open_node_queue"]:
        node = BranchNode(
            node_id=raw_node["node_id"],
            parent_id=raw_node["parent"],
            depth=1,
            branch_history=(_row(raw_node["constraint"]),),
            inherited_lb=float(raw_node["inherited_valid_lower_bound"]),
        )
        result = solve_fully_priced_node(
            data,
            registry,
            node,
            workers=args.workers,
            pricing_time_limit_seconds=args.pricing_time_limit,
        )
        node_payloads.append(_write_node(run_dir, result))
        registry.extend(result.generated_columns)
        print(
            f"{node.node_id} status={result.status} LB={result.fully_priced_lb} "
            f"iterations={len(result.iterations)} generated={len(result.generated_columns)}",
            flush=True,
        )

    all_fully_priced = all(
        payload["status"] == "FULLY_PRICED" for payload in node_payloads
    )
    global_lb = min(
        float(payload["effective_rigorous_lb"]) for payload in node_payloads
    )
    write_json(
        run_dir / "global-column-registry.json",
        {
            "initial_columns": initial_registry_count,
            "current_columns": len(registry),
            "new_columns": [
                asdict(column) for column in registry[initial_registry_count:]
            ],
        },
    )
    checkpoint = {
        "status": (
            "ROOT_CHILDREN_FULLY_PRICED"
            if all_fully_priced else "BRANCH_AND_PRICE_INCOMPLETE"
        ),
        "global_optimality_claim": False,
        "incumbent_ub": 14730,
        "global_lb": global_lb,
        "rigorous_gap_over_ub": (14730 - global_lb) / 14730,
        "processed_fully_priced_nodes": 1 + sum(
            payload["status"] == "FULLY_PRICED" for payload in node_payloads
        ),
        "global_column_registry_size": len(registry),
        "root_children": node_payloads,
        "source_sha": _git("rev-parse", "HEAD"),
        "source_dirty": bool(_git("status", "--porcelain")),
        "pricing_time_limit_seconds": args.pricing_time_limit,
    }
    write_json(run_dir / "checkpoint.json", checkpoint)
    print(
        f"ROOT CHILD GATE status={checkpoint['status']} global_lb={global_lb} "
        f"columns={len(registry)} run={run_dir}"
    )
    return 0 if all_fully_priced else 3


if __name__ == "__main__":
    raise SystemExit(main())
