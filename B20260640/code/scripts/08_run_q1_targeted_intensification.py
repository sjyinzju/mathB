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

from src.io_utils import sha256, write_csv, write_json
from src.solver import (
    SolverCache,
    exact_targeted_repair,
    export_q1_solution,
    load_problem_data,
    load_q1_solution,
    route_elimination_audit,
    targeted_route_indices,
)
from src.validation import validate_solution


def _git(*args: str) -> str | None:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _result_row(attempt: int, result, accepted: bool) -> dict[str, object]:
    return {
        "attempt": attempt,
        "reason": result.reason,
        "route_indices": list(result.route_indices),
        "routes_before": result.routes_before,
        "routes_after": result.routes_after,
        "passengers_affected": result.passengers_affected,
        "aircraft_time_before": result.aircraft_time_before,
        "aircraft_time_after": result.aircraft_time_after,
        "route_count_delta": (
            result.routes_after - result.routes_before
            if result.routes_after is not None
            else None
        ),
        "local_aircraft_time_delta": (
            result.aircraft_time_after - result.aircraft_time_before
            if result.aircraft_time_after is not None
            else None
        ),
        "solve_time": result.elapsed_seconds,
        "accepted": accepted,
        **result.diagnostics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Q1 targeted elimination/high-impact/block/cross-exchange exact neighborhoods"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--start-dir", type=Path, required=True)
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "outputs" / "q1" / "final-or"
    )
    parser.add_argument("--repair-seconds", type=float, default=20.0)
    parser.add_argument("--max-service-nodes", type=int, default=3)
    args = parser.parse_args()

    run_dir = args.output_root / args.run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()
    data = load_problem_data()
    current = load_q1_solution(
        args.start_dir / "q1-routes.csv",
        args.start_dir / "q1-assignments.csv",
        data,
        method="q1_targeted_start",
    )
    initial = current
    cache = SolverCache(data)
    initial_audit = route_elimination_audit(current, data)
    write_json(run_dir / "route-elimination-audit-initial.json", list(initial_audit))

    schedule = [
        ("cross_exchange", 2, 5),
        ("high_impact", 6, 5),
        ("facility_block", 7, 5),
        ("targeted_large_8", 8, 3),
        ("targeted_large_9", 9, 3),
        ("route_elimination_10", 10, 5),
    ]
    logs: list[dict[str, object]] = []
    candidate_events: list[dict[str, object]] = []
    memory: set[tuple[str, tuple[int, ...]]] = set()
    best_88 = None
    new_best_count = 0
    attempt = 0
    for reason, size, source_count in schedule:
        audit = route_elimination_audit(current, data)
        mode = (
            "facility_block"
            if reason == "facility_block"
            else "cross_exchange"
            if reason == "cross_exchange"
            else "high_impact"
        )
        for source_row in audit[:source_count]:
            indices = targeted_route_indices(
                current,
                data,
                int(source_row["route_index"]),
                min(size, len(current.routes)),
                mode=mode,
            )
            fingerprint = (reason, indices)
            if fingerprint in memory:
                continue
            memory.add(fingerprint)
            attempt += 1
            result = exact_targeted_repair(
                current,
                data,
                indices,
                reason=reason,
                seed=1000 + attempt,
                max_service_nodes=args.max_service_nodes,
                max_long_service_orders=160,
                repair_time_limit_seconds=args.repair_seconds,
                cache=cache,
            )
            accepted = bool(
                result.solution is not None
                and result.solution.metrics.comparison_key()
                < current.metrics.comparison_key()
            )
            row = _result_row(attempt, result, accepted)
            if result.solution is not None:
                row["candidate_objective"] = (
                    result.solution.metrics.total_aircraft_time_minutes
                )
                row["candidate_flights"] = result.solution.metrics.total_flights
                if result.solution.metrics.total_flights <= 88 and (
                    best_88 is None
                    or result.solution.metrics.comparison_key()
                    < best_88.metrics.comparison_key()
                ):
                    best_88 = result.solution
            if accepted:
                current = result.solution
                new_best_count += 1
                best_dir = run_dir / "new-bests" / f"best-{new_best_count:03d}"
                export_q1_solution(
                    current,
                    best_dir / "q1-routes.csv",
                    best_dir / "q1-assignments.csv",
                )
                validation = validate_solution(
                    "q1",
                    best_dir / "q1-routes.csv",
                    best_dir / "q1-assignments.csv",
                    data_dir=ROOT / "data" / "raw",
                    config=data.config,
                )
                if not validation.valid:
                    raise RuntimeError("Targeted new-best failed independent Validator")
                write_json(best_dir / "validator.json", validation.to_dict())
                write_json(best_dir / "source-attempt.json", row)
            row["current_objective_after"] = current.metrics.total_aircraft_time_minutes
            logs.append(row)
            candidate_events.append(
                {
                    "run_id": args.run_id,
                    "seed": 1000 + attempt,
                    "warm_start": str(args.start_dir),
                    "parent_solution": initial.metrics.total_aircraft_time_minutes,
                    "basin_lineage": "master->targeted_exact",
                    "algorithm": "q1_targeted_exact_repair",
                    "candidate_evaluated": True,
                    "feasible": result.solution is not None,
                    "repair_selected": accepted,
                    "repair_accepted": accepted,
                    "primary_improvement": bool(
                        result.diagnostics.get("primary_improvement", False)
                    ),
                    "new_best": accepted,
                    "actual_delta_aircraft_time": result.diagnostics.get(
                        "actual_delta_aircraft_time"
                    ),
                    "evaluation_cost": result.elapsed_seconds,
                    "label": result.diagnostics.get("label", "INVALID"),
                    **result.diagnostics.get("pre_features", {}),
                }
            )

    routes_path = run_dir / "q1-routes.csv"
    assignments_path = run_dir / "q1-assignments.csv"
    export_q1_solution(current, routes_path, assignments_path)
    validation = validate_solution(
        "q1",
        routes_path,
        assignments_path,
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    if not validation.valid:
        raise RuntimeError("Final targeted solution failed Validator")
    write_json(run_dir / "validator.json", validation.to_dict())
    final_metrics = validation.metrics.to_dict()
    write_json(
        run_dir / "metrics.json",
        {
            "gate_pass": True,
            "initial_metrics": initial.metrics.to_dict(),
            "validator_metrics": final_metrics,
            "new_best_count": new_best_count,
            "attempt_count": attempt,
            "lowest_flights_found": (
                best_88.metrics.total_flights if best_88 is not None else current.metrics.total_flights
            ),
            "best_88_metrics": best_88.metrics.to_dict() if best_88 is not None else None,
        },
    )
    if best_88 is not None:
        best_88_dir = run_dir / "best-88"
        export_q1_solution(
            best_88,
            best_88_dir / "q1-routes.csv",
            best_88_dir / "q1-assignments.csv",
        )
        best_88_validation = validate_solution(
            "q1",
            best_88_dir / "q1-routes.csv",
            best_88_dir / "q1-assignments.csv",
            data_dir=ROOT / "data" / "raw",
            config=data.config,
        )
        write_json(best_88_dir / "validator.json", best_88_validation.to_dict())
    write_json(run_dir / "search-log.json", logs)
    write_json(run_dir / "candidate-events.json", candidate_events)
    final_audit = route_elimination_audit(current, data)
    write_json(run_dir / "route-elimination-audit-final.json", list(final_audit))
    write_csv(
        run_dir / "neighborhood-summary.csv",
        [
            "attempt",
            "reason",
            "routes_before",
            "routes_after",
            "passengers_affected",
            "aircraft_time_before",
            "aircraft_time_after",
            "route_count_delta",
            "local_aircraft_time_delta",
            "solve_time",
            "accepted",
            "candidate_objective",
            "candidate_flights",
            "current_objective_after",
        ],
        logs,
    )
    write_json(
        run_dir / "run_config.json",
        {
            "run_id": args.run_id,
            "method": "targeted exact high-impact/block/cross-exchange/route-elimination",
            "git_commit": _git("rev-parse", "HEAD"),
            "git_branch": _git("branch", "--show-current"),
            "git_dirty": bool(_git("status", "--porcelain")),
            "source": {
                "directory": str(args.start_dir.resolve()),
                "routes_sha256": sha256(args.start_dir / "q1-routes.csv"),
                "assignments_sha256": sha256(args.start_dir / "q1-assignments.csv"),
            },
            "repair_seconds": args.repair_seconds,
            "max_service_nodes": args.max_service_nodes,
            "schedule": schedule,
            "duplicate_neighborhood_memory_size": len(memory),
            "cache": cache.stats(),
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        },
    )
    print(
        "Q1 TARGETED PASS: "
        f"initial={initial.metrics.total_aircraft_time_minutes}, "
        f"final={final_metrics['total_aircraft_time_minutes']}, "
        f"flights={final_metrics['total_flights']}, "
        f"new_best={new_best_count}, attempts={attempt}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
