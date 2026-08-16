from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_json
from src.solver import (
    SolverCache,
    exact_targeted_repair,
    export_q1_solution,
    load_problem_data,
    load_q1_solution,
    route_identity,
    targeted_route_indices,
)
from src.validation import validate_solution


def _git(*args: str) -> str | None:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _identity_distance(left, right) -> int:
    left_keys = {route_identity(route) for route in left.routes}
    right_keys = {route_identity(route) for route in right.routes}
    return len(left_keys ^ right_keys)


def main() -> int:
    parser = argparse.ArgumentParser(description="Q1 bounded elite difference-region path relinking")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--start-dir", type=Path, required=True)
    parser.add_argument("--guide-dir", type=Path, required=True)
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "outputs" / "q1" / "final-or"
    )
    parser.add_argument("--repair-seconds", type=float, default=10.0)
    parser.add_argument("--max-steps", type=int, default=12)
    args = parser.parse_args()

    run_dir = args.output_root / args.run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()
    data = load_problem_data()
    start = load_q1_solution(
        args.start_dir / "q1-routes.csv",
        args.start_dir / "q1-assignments.csv",
        data,
        method="q1_path_start",
    )
    guide = load_q1_solution(
        args.guide_dir / "q1-routes.csv",
        args.guide_dir / "q1-assignments.csv",
        data,
        method="q1_path_guide",
    )
    working = start
    best = start
    cache = SolverCache(data)
    guide_keys = {route_identity(route) for route in guide.routes}
    guide_unique = [
        route
        for route in guide.routes
        if route_identity(route) not in {route_identity(item) for item in start.routes}
    ]
    logs: list[dict[str, object]] = []
    for step, guide_route in enumerate(guide_unique[: args.max_steps], start=1):
        guide_facilities = tuple(dict.fromkeys(guide_route.service_facilities))
        current_keys = [route_identity(route) for route in working.routes]
        candidates = [
            index for index, key in enumerate(current_keys) if key not in guide_keys
        ] or list(range(len(working.routes)))
        source_index = min(
            candidates,
            key=lambda index: (
                min(
                    data.matrix[left][right]
                    for left in working.routes[index].service_facilities
                    for right in guide_facilities
                ),
                index,
            ),
        )
        size = min(4 + 2 * (step % 2), len(working.routes))
        indices = targeted_route_indices(
            working,
            data,
            source_index,
            size,
            mode="cross_exchange",
        )
        before_distance = _identity_distance(working, guide)
        result = exact_targeted_repair(
            working,
            data,
            indices,
            reason="path_relink",
            seed=2000 + step,
            max_service_nodes=3,
            max_long_service_orders=160,
            repair_time_limit_seconds=args.repair_seconds,
            cache=cache,
        )
        candidate = result.solution
        after_distance = (
            _identity_distance(candidate, guide) if candidate is not None else None
        )
        follows_path = bool(
            candidate is not None
            and after_distance is not None
            and after_distance < before_distance
        )
        accepted_working = bool(
            candidate is not None
            and (
                follows_path
                or candidate.metrics.comparison_key() < working.metrics.comparison_key()
            )
            and candidate.metrics.total_aircraft_time_minutes
            <= max(start.metrics.total_aircraft_time_minutes, guide.metrics.total_aircraft_time_minutes) + 20
        )
        new_best = bool(
            candidate is not None
            and candidate.metrics.comparison_key() < best.metrics.comparison_key()
        )
        if new_best:
            best = candidate
        if accepted_working:
            working = candidate
        logs.append(
            {
                "step": step,
                "guide_service_order": list(guide_route.service_facilities),
                "route_indices": list(indices),
                "before_identity_distance": before_distance,
                "after_identity_distance": after_distance,
                "follows_path": follows_path,
                "accepted_working": accepted_working,
                "new_best": new_best,
                "candidate_objective": candidate.metrics.total_aircraft_time_minutes
                if candidate is not None
                else None,
                "candidate_flights": candidate.metrics.total_flights
                if candidate is not None
                else None,
                "solve_time": result.elapsed_seconds,
                "label": result.diagnostics.get("label"),
            }
        )

    routes_path = run_dir / "q1-routes.csv"
    assignments_path = run_dir / "q1-assignments.csv"
    export_q1_solution(best, routes_path, assignments_path)
    validation = validate_solution(
        "q1",
        routes_path,
        assignments_path,
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    if not validation.valid:
        raise RuntimeError("Path-relinking best failed Validator")
    write_json(run_dir / "validator.json", validation.to_dict())
    write_json(
        run_dir / "metrics.json",
        {
            "gate_pass": True,
            "start_metrics": start.metrics.to_dict(),
            "guide_metrics": guide.metrics.to_dict(),
            "best_metrics": validation.metrics.to_dict(),
            "initial_identity_distance": _identity_distance(start, guide),
            "final_working_identity_distance": _identity_distance(working, guide),
            "steps": len(logs),
            "path_steps": sum(bool(row["follows_path"]) for row in logs),
            "new_best_steps": sum(bool(row["new_best"]) for row in logs),
        },
    )
    write_json(run_dir / "path-log.json", logs)
    write_json(
        run_dir / "run_config.json",
        {
            "run_id": args.run_id,
            "method": "bounded difference-region path relinking + exact local repair",
            "git_commit": _git("rev-parse", "HEAD"),
            "git_branch": _git("branch", "--show-current"),
            "git_dirty": bool(_git("status", "--porcelain")),
            "repair_seconds": args.repair_seconds,
            "max_steps": args.max_steps,
            "cache": cache.stats(),
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        },
    )
    print(
        "Q1 PATH RELINK PASS: "
        f"start={start.metrics.total_aircraft_time_minutes}, "
        f"guide={guide.metrics.total_aircraft_time_minutes}, "
        f"best={validation.metrics.total_aircraft_time_minutes}, "
        f"path_steps={sum(bool(row['follows_path']) for row in logs)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

