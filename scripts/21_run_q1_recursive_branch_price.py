from __future__ import annotations

import argparse
import csv
import heapq
import json
import math
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import sha256, write_csv, write_json
from src.solver import (
    CERT_TOL,
    ArcBranchRow,
    BranchNode,
    Q1MasterConfig,
    build_frozen_incumbent_start,
    collect_elite_route_pool,
    exact_column_start_from_pattern_start,
    initial_exact_columns,
    load_generated_exact_columns,
    load_problem_data,
    load_q1_solution,
    materialize_exact_column_solution,
    solve_exact_column_integer_master,
    solve_fully_priced_node,
)
from src.solver.exporter import export_q1_solution
from src.validation import validate_solution


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _row(raw) -> ArcBranchRow:
    return ArcBranchRow(tuple(raw["arc"]), raw["sense"], int(raw["rhs"]))


def _node_payload(node: BranchNode) -> dict[str, object]:
    return {
        "node_id": node.node_id,
        "parent_id": node.parent_id,
        "depth": node.depth,
        "branch_history": [asdict(row) for row in node.branch_history],
        "inherited_lb": node.inherited_lb,
        "status": "OPEN_UNPRICED",
        "bound_provenance": "INHERITED",
    }


def _node_from_payload(raw) -> BranchNode:
    return BranchNode(
        node_id=raw["node_id"],
        parent_id=raw.get("parent_id"),
        depth=int(raw["depth"]),
        branch_history=tuple(_row(row) for row in raw["branch_history"]),
        inherited_lb=float(raw["inherited_lb"]),
    )


def _child_id(parent: BranchNode, side: str) -> str:
    suffix = parent.node_id.split("-", 1)[1] if "-" in parent.node_id else ""
    return f"N{parent.depth + 1}-{suffix}{side}"


def _children(parent: BranchNode, arc, value, lb) -> tuple[BranchNode, BranchNode]:
    lower = math.floor(value)
    upper = math.ceil(value)
    return (
        BranchNode(
            _child_id(parent, "L"), parent.node_id, parent.depth + 1,
            parent.branch_history + (ArcBranchRow(tuple(arc), "<=", lower),),
            float(lb),
        ),
        BranchNode(
            _child_id(parent, "R"), parent.node_id, parent.depth + 1,
            parent.branch_history + (ArcBranchRow(tuple(arc), ">=", upper),),
            float(lb),
        ),
    )


def _initial_open_nodes(source_dir: Path) -> list[BranchNode]:
    open_nodes = []
    for node_id in ("N1-L", "N1-R"):
        summary = json.loads((source_dir / node_id / "summary.json").read_text(encoding="utf-8"))
        parent = BranchNode(
            node_id=node_id,
            parent_id=summary["parent_id"],
            depth=int(summary["depth"]),
            branch_history=tuple(_row(row) for row in summary["branch_history"]),
            inherited_lb=float(summary["fully_priced_lb"]),
        )
        selected = summary["selected_branch"]
        open_nodes.extend(
            _children(
                parent,
                selected["arc"],
                float(selected["value"]),
                float(summary["fully_priced_lb"]),
            )
        )
    return open_nodes


def _write_iteration_artifacts(node_dir: Path, result) -> None:
    for record in result.iterations:
        iteration_dir = node_dir / f"iteration-{record.iteration:03d}"
        iteration_dir.mkdir()
        write_json(
            iteration_dir / "rmp.json",
            {
                "phase": record.phase,
                "status": record.rmp.status,
                "objective": record.rmp.objective,
                "proven_optimal": record.rmp.proven_optimal,
                "demand_duals": {
                    f"{o}->{d}": value
                    for (o, d), value in record.rmp.demand_duals.items()
                },
                "branch_duals": [
                    {"row": asdict(row), "canonical_dual": value}
                    for row, value in record.rmp.branch_duals.items()
                ],
            },
        )
        for pricing in record.pricing_results:
            write_json(
                iteration_dir / f"pricing-{pricing.base_airport}-{pricing.aircraft_type}.json",
                asdict(pricing),
            )
        write_json(
            iteration_dir / "summary.json",
            {
                "phase": record.phase,
                "rmp_objective": record.rmp.objective,
                "added_column_ids": list(record.added_column_ids),
                "all_pricing_optimal": all(p.proven_optimal for p in record.pricing_results),
                "minimum_reduced_cost": min(
                    p.reduced_cost for p in record.pricing_results
                    if p.reduced_cost is not None
                ),
                "elapsed_seconds": record.elapsed_seconds,
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume exact recursive Q1 Branch-and-Price")
    parser.add_argument("--run-id", default="20260816-recursive-best-bound")
    parser.add_argument("--max-new-nodes", type=int, default=1)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--node-mip-seconds", type=float, default=30.0)
    args = parser.parse_args()
    run_dir = ROOT / "outputs" / "q1" / "exact" / "branch-and-price" / args.run_id
    source_dir = ROOT / "outputs" / "q1" / "exact" / "branch-and-price" / "20260816-root-children"
    checkpoint_path = run_dir / "checkpoint.json"
    run_dir.mkdir(parents=True, exist_ok=True)

    data = load_problem_data()
    pool = collect_elite_route_pool(data, ROOT / "outputs" / "q1")
    registry = list(initial_exact_columns(data, pool))
    registry.extend(load_generated_exact_columns(
        ROOT / "outputs" / "q1" / "exact" / "column-generation"
        / "20260816-fullspace-cg" / "checkpoint.json"
    ))
    processed = []
    progress = []
    incumbent_ub = 14730
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        registry.extend(load_generated_exact_columns(checkpoint_path))
        open_nodes = [_node_from_payload(raw) for raw in checkpoint["open_node_queue"]]
        processed = list(checkpoint["processed_nodes"])
        progress = list(checkpoint["progress"])
        incumbent_ub = int(checkpoint["global_ub"])
    else:
        registry.extend(load_generated_exact_columns(source_dir / "global-column-registry.json"))
        open_nodes = _initial_open_nodes(source_dir)

    frozen = load_q1_solution(
        ROOT / "outputs" / "q1" / "final" / "q1-routes.csv",
        ROOT / "outputs" / "q1" / "final" / "q1-assignments.csv",
        data,
    )
    _, pattern_start = build_frozen_incumbent_start(data, pool, frozen, Q1MasterConfig())
    initial_root_columns = 1041
    initial_run_registry = len(registry)
    new_nodes_processed = 0
    search_started = time.perf_counter()

    while open_nodes and new_nodes_processed < args.max_new_nodes:
        heap = [(node.inherited_lb, node.depth, node.node_id, node) for node in open_nodes]
        heapq.heapify(heap)
        _, _, _, node = heapq.heappop(heap)
        open_nodes = [candidate for candidate in open_nodes if candidate.node_id != node.node_id]
        node_dir = run_dir / node.node_id
        # exist_ok: a killed earlier resume can leave the empty node directory
        # behind without ever writing its checkpoint record; iteration
        # artifacts are idempotent JSON/CSV overwrites.
        node_dir.mkdir(exist_ok=True)
        result = solve_fully_priced_node(data, registry, node, workers=args.workers)
        registry.extend(result.generated_columns)
        _write_iteration_artifacts(node_dir, result)

        fathom_reason = None
        children = ()
        validation = None
        integer_heuristic = None
        if result.status == "FULLY_PRICED_INFEASIBLE":
            fathom_reason = "FATHOM_BY_INFEASIBILITY"
        elif result.status == "FULLY_PRICED":
            assert result.fully_priced_lb is not None
            if math.ceil(result.fully_priced_lb - CERT_TOL) >= incumbent_ub:
                fathom_reason = "FATHOM_BY_BOUND"
            elif result.fractional_route_variables == 0:
                solution = materialize_exact_column_solution(data, registry, result.selected_values)
                candidate_dir = node_dir / "integral-lp-candidate"
                candidate_dir.mkdir()
                routes = candidate_dir / "q1-routes.csv"
                assignments = candidate_dir / "q1-assignments.csv"
                export_q1_solution(solution, routes, assignments)
                validation = validate_solution("q1", routes, assignments, data_dir=ROOT / "data" / "raw")
                write_json(candidate_dir / "validator.json", validation.to_dict())
                if validation.valid:
                    incumbent_ub = min(incumbent_ub, solution.metrics.total_aircraft_time_minutes)
                    fathom_reason = "FATHOM_BY_INTEGRALITY"
            else:
                start = exact_column_start_from_pattern_start(pattern_start.values, len(registry))
                heuristic = solve_exact_column_integer_master(
                    data,
                    registry,
                    mip_start_values=start,
                    branch_history=node.branch_history,
                    primary_upper_bound_minutes=incumbent_ub - 1,
                    time_limit_seconds=args.node_mip_seconds,
                    log_path=node_dir / "integer-heuristic.log",
                    output_flag=True,
                )
                integer_heuristic = {
                    "outcome": heuristic.outcome,
                    "objective": heuristic.objective,
                    "model_status": heuristic.model_status,
                    "elapsed_seconds": heuristic.elapsed_seconds,
                    "mip_start_feasible_for_model": heuristic.mip_start_feasible_for_model,
                }
                if heuristic.solution is not None:
                    candidate_dir = node_dir / "integer-heuristic-candidate"
                    candidate_dir.mkdir()
                    routes = candidate_dir / "q1-routes.csv"
                    assignments = candidate_dir / "q1-assignments.csv"
                    export_q1_solution(heuristic.solution, routes, assignments)
                    validation = validate_solution("q1", routes, assignments, data_dir=ROOT / "data" / "raw")
                    write_json(candidate_dir / "validator.json", validation.to_dict())
                    if validation.valid and heuristic.objective < incumbent_ub:
                        incumbent_ub = int(heuristic.objective)
                if result.selected_branch is None:
                    raise RuntimeError("Fractional fully-priced node has no arc branch")
                children = _children(
                    node,
                    result.selected_branch[0],
                    result.selected_branch[1],
                    result.fully_priced_lb,
                )
                open_nodes.extend(children)
        else:
            # Preserve an incomplete node in the queue with its conservative
            # inherited bound; never lose it or pretend the tree is closed.
            open_nodes.append(node)

        record = {
            "node_id": node.node_id,
            "parent_id": node.parent_id,
            "depth": node.depth,
            "branch_history": [asdict(row) for row in node.branch_history],
            "status": result.status,
            "inherited_lb": node.inherited_lb,
            "fully_priced_lb": result.fully_priced_lb,
            "effective_rigorous_lb": result.effective_rigorous_lb,
            "fathom_reason": fathom_reason,
            "fractional_route_variables": result.fractional_route_variables,
            "selected_branch": (
                {"arc": list(result.selected_branch[0]), "value": result.selected_branch[1]}
                if result.selected_branch else None
            ),
            "cg_iterations": len(result.iterations),
            "pricing_calls": result.pricing_calls,
            "generated_columns": len(result.generated_columns),
            "rmp_seconds": result.rmp_seconds,
            "pricing_seconds": result.pricing_seconds,
            "elapsed_seconds": result.elapsed_seconds,
            "integer_heuristic": integer_heuristic,
            "validator": validation.to_dict() if validation else None,
            "children": [child.node_id for child in children],
        }
        processed.append(record)
        write_json(node_dir / "summary.json", record)
        new_nodes_processed += 1

        global_lb = min(
            (candidate.inherited_lb for candidate in open_nodes),
            default=float(incumbent_ub),
        )
        progress.append(
            {
                "elapsed_seconds": round(time.perf_counter() - search_started, 6),
                "processed_nodes": 3 + len(processed),
                "open_nodes": len(open_nodes),
                "global_lb": global_lb,
                "global_ub": incumbent_ub,
                "rigorous_gap_over_ub": (incumbent_ub - global_lb) / incumbent_ub,
                "global_columns": len(registry),
                "total_generated_after_root": len(registry) - initial_root_columns,
            }
        )
        checkpoint = {
            "status": "BRANCH_AND_PRICE_INCOMPLETE" if open_nodes else "TREE_CLOSED",
            "global_ub": incumbent_ub,
            "global_lb": global_lb,
            "processed_nodes": processed,
            "open_node_queue": [_node_payload(candidate) for candidate in open_nodes],
            "progress": progress,
            "generated_columns": [asdict(column) for column in registry[initial_root_columns:]],
            "global_column_registry_size": len(registry),
            "node_selection": "best-bound-first, then shallow depth, then node_id",
            "source_sha": _git("rev-parse", "HEAD"),
            "source_dirty": bool(_git("status", "--porcelain")),
        }
        write_json(checkpoint_path, checkpoint)
        print(
            f"processed={node.node_id} status={result.status} LB={result.fully_priced_lb} "
            f"open={len(open_nodes)} globalLB={global_lb} UB={incumbent_ub}",
            flush=True,
        )

    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    node_fields = [
        "node_id", "parent_id", "depth", "status", "inherited_lb",
        "fully_priced_lb", "effective_rigorous_lb", "fathom_reason",
        "fractional_route_variables", "cg_iterations", "pricing_calls",
        "generated_columns", "rmp_seconds", "pricing_seconds", "elapsed_seconds",
    ]
    write_csv(
        run_dir / "nodes.csv",
        node_fields,
        ({key: row.get(key) for key in node_fields} for row in processed),
    )
    progress_fields = [
        "elapsed_seconds", "processed_nodes", "open_nodes", "global_lb",
        "global_ub", "rigorous_gap_over_ub", "global_columns",
        "total_generated_after_root",
    ]
    write_csv(run_dir / "progress.csv", progress_fields, progress)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
