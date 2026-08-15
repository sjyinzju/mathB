from __future__ import annotations

import argparse
import hashlib
import os
import platform
import pickle
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import scipy

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_json
from src.solver import (
    Q2MasterConfig,
    SolverCache,
    build_q2_variant_pool,
    candidate_pool_hash,
    candidate_service_sequences,
    export_q1_solution,
    load_problem_data,
    load_q1_solution,
    load_q2_solution,
    solve_q2_master,
)
from src.validation import validate_solution


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_value(*args: str) -> str | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _comparison_key(metrics: dict[str, object]) -> tuple[float, ...]:
    return (
        float(metrics["total_aircraft_time_minutes"]),
        float(metrics["total_passenger_travel_time_minutes"]),
        float(metrics["total_flights"]),
        float(metrics["total_fuel_consumption_kg"]),
        -float(metrics["seat_utilization"]),
    )


def _master_bounds(solution) -> dict[str, object] | None:
    diagnostics = solution.diagnostics.get("q2_master")
    if not diagnostics:
        return None
    return {
        "bound_scope": "restricted_master",
        "scope_note": (
            "The incumbent, dual bound and MIP gap certify only this finite "
            "candidate-route master, not the unrestricted Q2 problem."
        ),
        "primary_incumbent_minutes": diagnostics["primary_objective"],
        "primary_dual_bound_minutes": diagnostics["primary_dual_bound"],
        "primary_mip_gap": diagnostics["primary_mip_gap"],
        "primary_status": diagnostics["primary_status"],
        "primary_proven_optimal": diagnostics["primary_proven_optimal"],
        "secondary_status": diagnostics["secondary_status"],
        "secondary_mip_gap": diagnostics["secondary_mip_gap"],
        "secondary_proven_optimal": diagnostics["secondary_proven_optimal"],
        "candidate_sequences": diagnostics["candidate_sequences"],
        "candidate_variants": diagnostics["candidate_variants"],
        "compatible_assignments": diagnostics["compatible_assignments"],
        "candidate_pool_hash": diagnostics["candidate_pool_hash"],
        "primary_time_limit_seconds": diagnostics["primary_time_limit_seconds"],
        "secondary_time_limit_seconds": diagnostics["secondary_time_limit_seconds"],
        "primary_elapsed_seconds": diagnostics["primary_elapsed_seconds"],
        "secondary_elapsed_seconds": diagnostics["secondary_elapsed_seconds"],
        "final_objectives": diagnostics["final_objectives"],
        "gap_definition": "(incumbent-dual_bound)/abs(incumbent)",
    }


def _atomic_promote(run_dir: Path, best_dir: Path) -> None:
    if run_dir.resolve() == best_dir.resolve():
        raise ValueError("run directory and best directory must differ")
    token = uuid.uuid4().hex
    staged = best_dir.parent / f".{best_dir.name}.staged-{token}"
    backup = best_dir.parent / f".{best_dir.name}.backup-{token}"
    shutil.copytree(run_dir, staged)

    def replace_with_retry(source: Path, destination: Path) -> None:
        last_error: PermissionError | None = None
        for attempt in range(6):
            try:
                os.replace(source, destination)
                return
            except PermissionError as error:
                last_error = error
                time.sleep(0.1 * (attempt + 1))
        assert last_error is not None
        raise last_error

    try:
        if best_dir.exists():
            replace_with_retry(best_dir, backup)
        replace_with_retry(staged, best_dir)
    except Exception:
        if not best_dir.exists() and backup.exists():
            replace_with_retry(backup, best_dir)
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
        if backup.exists():
            shutil.rmtree(backup)


def _write_solution(solution, run_dir: Path, data):
    routes_path = run_dir / "q2-routes.csv"
    assignments_path = run_dir / "q2-assignments.csv"
    export_q1_solution(solution, routes_path, assignments_path)
    validation = validate_solution(
        "q2",
        routes_path,
        assignments_path,
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    write_json(run_dir / "q2-validator.json", validation.to_dict())
    return validation


def main() -> int:
    parser = argparse.ArgumentParser(description="Q2 restricted candidate-route master")
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "q2")
    parser.add_argument("--q1-routes", type=Path, default=ROOT / "outputs/q1/best/q1-routes.csv")
    parser.add_argument(
        "--q1-assignments",
        type=Path,
        default=ROOT / "outputs/q1/best/q1-assignments.csv",
    )
    parser.add_argument("--q1-source-ref", default="main:outputs/q1/best")
    parser.add_argument("--nearest-neighbors", type=int, default=3)
    parser.add_argument("--high-demand-nodes", type=int, default=10)
    parser.add_argument("--primary-time-limit", type=float, default=195.0)
    parser.add_argument("--secondary-time-limit", type=float, default=105.0)
    parser.add_argument("--primary-only", action="store_true")
    parser.add_argument("--mip-gap", type=float, default=0.0)
    parser.add_argument(
        "--variant-cache",
        type=Path,
        help="Optional trusted, run-local cache; never committed or used as a result source",
    )
    parser.add_argument("--replay-routes", type=Path)
    parser.add_argument("--replay-assignments", type=Path)
    parser.add_argument("--replay-source-ref")
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--promote-baseline", action="store_true")
    args = parser.parse_args()

    if bool(args.replay_routes) != bool(args.replay_assignments):
        raise ValueError("--replay-routes and --replay-assignments must be provided together")
    secondary_limit = 0.0 if args.primary_only else args.secondary_time_limit
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q2-rmp"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()
    data = load_problem_data()
    cache = SolverCache(data)
    sequences = ()
    variants = ()
    pool_hash = None
    variant_seconds = 0.0
    variant_cache_loaded = False

    if args.replay_routes:
        solution = load_q2_solution(
            args.replay_routes,
            args.replay_assignments,
            data,
            method="q2_canonical_19736_replay",
        )
    else:
        variant_started = time.perf_counter()
        q1_routes_sha = _sha256(args.q1_routes)
        q1_assignments_sha = _sha256(args.q1_assignments)
        expected_cache_key = {
            "q1_routes_sha256": q1_routes_sha,
            "q1_assignments_sha256": q1_assignments_sha,
            "nearest_neighbors": args.nearest_neighbors,
            "high_demand_nodes": args.high_demand_nodes,
            "q2_source_sha256": _sha256(ROOT / "src" / "solver" / "q2.py"),
        }
        cached = None
        if args.variant_cache and args.variant_cache.exists():
            with args.variant_cache.open("rb") as stream:
                candidate = pickle.load(stream)
            if candidate.get("cache_key") == expected_cache_key:
                cached = candidate
        if cached is not None:
            sequences = tuple(cached["sequences"])
            variants = tuple(cached["variants"])
            variant_cache_loaded = True
        else:
            q1 = load_q1_solution(args.q1_routes, args.q1_assignments, data)
            sequences = candidate_service_sequences(
                data,
                seed_routes=q1.routes,
                nearest_neighbors=args.nearest_neighbors,
                high_demand_nodes=args.high_demand_nodes,
            )
            variants = build_q2_variant_pool(data, sequences, cache=cache)
            if args.variant_cache:
                args.variant_cache.parent.mkdir(parents=True, exist_ok=True)
                with args.variant_cache.open("wb") as stream:
                    pickle.dump(
                        {
                            "cache_key": expected_cache_key,
                            "sequences": sequences,
                            "variants": variants,
                        },
                        stream,
                    )
        variant_seconds = time.perf_counter() - variant_started
        pool_hash = candidate_pool_hash(variants)
        solution = solve_q2_master(
            data,
            variants,
            config=Q2MasterConfig(
                nearest_neighbors=args.nearest_neighbors,
                high_demand_nodes=args.high_demand_nodes,
                primary_time_limit_seconds=args.primary_time_limit,
                secondary_time_limit_seconds=secondary_limit,
                mip_relative_gap=args.mip_gap,
            ),
            method="q2_q2_1_restricted_route_master",
        )

    validation = _write_solution(solution, run_dir, data)
    validator_metrics = validation.metrics.to_dict() if validation.metrics else None
    internal_metrics = solution.metrics.to_dict()
    metrics_match = bool(
        validator_metrics
        and all(
            abs(float(validator_metrics[key]) - float(value)) <= 1.0e-6
            for key, value in internal_metrics.items()
        )
    )
    gate_pass = bool(
        validation.valid
        and validator_metrics
        and int(validator_metrics["served_passengers"]) == data.q2_passenger_count
        and metrics_match
    )
    elapsed = time.perf_counter() - started
    write_json(
        run_dir / "metrics.json",
        {
            "gate_pass": gate_pass,
            "metrics_match": metrics_match,
            "passenger_count": data.q2_passenger_count,
            "internal_metrics": internal_metrics,
            "validator_metrics": validator_metrics,
            "comparison_key": list(_comparison_key(validator_metrics)) if validator_metrics else None,
        },
    )
    write_json(run_dir / "q2-bounds.json", {"master": _master_bounds(solution)})
    q1_inputs = None
    if not args.replay_routes:
        q1_inputs = {
            "source_ref": args.q1_source_ref,
            "routes": str(args.q1_routes.resolve()),
            "assignments": str(args.q1_assignments.resolve()),
            "routes_sha256": _sha256(args.q1_routes),
            "assignments_sha256": _sha256(args.q1_assignments),
        }
    replay_inputs = None
    if args.replay_routes:
        replay_inputs = {
            "source_ref": args.replay_source_ref,
            "routes": None if args.replay_source_ref else str(args.replay_routes.resolve()),
            "assignments": (
                None if args.replay_source_ref else str(args.replay_assignments.resolve())
            ),
            "routes_sha256": _sha256(args.replay_routes),
            "assignments_sha256": _sha256(args.replay_assignments),
        }
    write_json(
        run_dir / "run_config.json",
        {
            "run_id": run_id,
            "method": solution.method,
            "git_commit": _git_value("rev-parse", "HEAD"),
            "git_branch": _git_value("branch", "--show-current"),
            "git_dirty": bool(_git_value("status", "--porcelain")),
            "runtime": {
                "python": platform.python_version(),
                "scipy": scipy.__version__,
                "backend": "scipy.optimize.milp/HiGHS",
            },
            "q1_inputs": q1_inputs,
            "replay_inputs": replay_inputs,
            "candidate_sequences": len(sequences),
            "candidate_variants": len(variants),
            "candidate_pool_hash": pool_hash,
            "variant_generation_seconds": round(variant_seconds, 6),
            "variant_cache": {
                "path": str(args.variant_cache.resolve()) if args.variant_cache else None,
                "loaded": variant_cache_loaded,
                "committable": False,
            },
            "primary_time_limit_seconds": args.primary_time_limit,
            "secondary_time_limit_seconds": secondary_limit,
            "mip_relative_gap": args.mip_gap,
            "total_elapsed_seconds": round(elapsed, 6),
            "cache": cache.stats(),
            "diagnostics": (
                {"loaded_from_ref": args.replay_source_ref}
                if args.replay_source_ref
                else solution.diagnostics
            ),
            "bound_scope": "restricted_master" if not args.replay_routes else None,
        },
    )
    if not gate_pass:
        print(f"Q2 GATE FAIL: {run_dir}", file=sys.stderr)
        return 2

    if args.promote:
        best_dir = args.output_root / "best"
        previous_metrics = None
        if (best_dir / "metrics.json").exists():
            import json

            previous_metrics = json.loads(
                (best_dir / "metrics.json").read_text(encoding="utf-8")
            )["validator_metrics"]
        if previous_metrics is None or _comparison_key(validator_metrics) < _comparison_key(previous_metrics):
            _atomic_promote(run_dir, best_dir)
    if args.promote_baseline:
        _atomic_promote(run_dir, args.output_root / "baseline-19736")

    print(
        "Q2 PASS: "
        f"time={validator_metrics['total_aircraft_time_minutes']} min, "
        f"passenger={validator_metrics['total_passenger_travel_time_minutes']} min, "
        f"flights={validator_metrics['total_flights']}, "
        f"fuel={validator_metrics['total_fuel_consumption_kg']} kg, "
        f"utilization={validator_metrics['seat_utilization']:.6f}, "
        f"elapsed={elapsed:.3f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
