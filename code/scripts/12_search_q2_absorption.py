from __future__ import annotations

import argparse
import hashlib
import json
import math
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

from src.io_utils import sha256, write_csv, write_json
from src.solver import (
    PromisingLocalMasterQueue,
    Q2LnsConfig,
    SolverCache,
    absorption_potential_ranking,
    audit_q2_solution,
    deepen_promising_master,
    exact_q2_local_repair,
    export_q1_solution,
    load_problem_data,
    load_q2_solution,
    q2_basin_fingerprint,
    select_absorption_neighborhood,
)
from src.validation import validate_solution


def _git_value(*args: str) -> str | None:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _key(solution) -> tuple[float, ...]:
    return solution.metrics.comparison_key()


def _materialize(solution, directory: Path, data) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=False)
    routes = directory / "q2-routes.csv"
    assignments = directory / "q2-assignments.csv"
    export_q1_solution(solution, routes, assignments)
    validation = validate_solution(
        "q2", routes, assignments, data_dir=ROOT / "data" / "raw", config=data.config
    )
    write_json(directory / "q2-validator.json", validation.to_dict())
    metrics = validation.metrics.to_dict() if validation.metrics else None
    internal = solution.metrics.to_dict()
    gate = bool(
        validation.valid
        and metrics
        and all(
            abs(float(metrics[key]) - float(value)) <= 1.0e-6
            for key, value in internal.items()
        )
    )
    write_json(
        directory / "metrics.json",
        {"gate_pass": gate, "internal_metrics": solution.metrics.to_dict(), "validator_metrics": metrics},
    )
    if not gate:
        raise RuntimeError(f"Validator gate failed for {directory}")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Targeted Q2 96-to-95 absorption search")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--start-dir", type=Path, default=ROOT / "outputs" / "q2" / "best"
    )
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "outputs" / "q2"
    )
    parser.add_argument("--top-sources", type=int, default=12)
    parser.add_argument("--six-route-sources", type=int, default=2)
    parser.add_argument("--candidate-budget", type=int, default=30)
    parser.add_argument("--local-primary-time", type=float, default=18.0)
    parser.add_argument("--deep-primary-time", type=float, default=45.0)
    parser.add_argument("--allowed-deterioration", type=int, default=800)
    parser.add_argument("--seed", type=int, default=300)
    parser.add_argument("--lineage-id", default="round3-absorption-control")
    parser.add_argument("--censored-log-limit", type=int, default=160)
    args = parser.parse_args()
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q2-r3-absorption"
    run_dir = args.output_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    data = load_problem_data()
    initial = load_q2_solution(
        args.start_dir / "q2-routes.csv",
        args.start_dir / "q2-assignments.csv",
        data,
        method="q2_round3_absorption_start",
    )
    best = initial
    best95 = None
    cache = SolverCache(data)
    queue = PromisingLocalMasterQueue()
    attempts: list[dict[str, object]] = []
    route_rows, pair_rows = audit_q2_solution(initial, data)
    ranking = absorption_potential_ranking(route_rows, pair_rows)
    config = Q2LnsConfig(
        iterations=0,
        neighborhood_size=4,
        source_pool_size=max(1, args.top_sources),
        target_pool_size=10,
        max_sequence_length=5,
        candidate_sequence_budget=args.candidate_budget,
        local_primary_seconds=args.local_primary_time,
        local_secondary_seconds=0.0,
        seed=args.seed,
        candidate_policy="geometry",
        operators=("flight_elimination",),
        targeted_four_stop=True,
        targeted_five_stop=True,
        run_purpose="optimization",
        lineage_id=args.lineage_id,
        censored_log_limit=args.censored_log_limit,
    )
    source_rows = ranking[: args.top_sources]
    candidate_stream = (run_dir / "candidate-log.jsonl").open("w", encoding="utf-8")
    for source_slot, source_row in enumerate(source_rows):
        source = int(source_row["route_index"])
        sizes = (4, 5, 6) if source_slot < args.six_route_sources else (4, 5)
        for size in sizes:
            neighborhood = select_absorption_neighborhood(
                source, pair_rows, route_count=size
            )
            facility_union = {
                node
                for index in neighborhood
                for node in initial.routes[index].service_facilities
            }
            universe_estimate = sum(
                math.factorial(len(facility_union))
                // math.factorial(len(facility_union) - length)
                for length in range(2, min(5, len(facility_union)) + 1)
            )
            if size == 6 and universe_estimate > 250_000:
                attempts.append(
                    {
                        "source_rank": source_slot + 1,
                        "source_route": source,
                        "neighborhood_size": size,
                        "neighborhood": json.dumps(neighborhood),
                        "candidate_sequences": universe_estimate,
                        "candidate_variants": 0,
                        "compatible_assignments": 0,
                        "status": "SKIPPED_POOL_EXPLOSION",
                        "restricted_bound": None,
                        "restricted_gap": None,
                        "repair_success": False,
                        "after_aircraft": None,
                        "after_flights": None,
                        "flight_delta": 0,
                        "global_new_best": False,
                        "best95": False,
                        "queued": False,
                        "runtime_seconds": 0.0,
                    }
                )
                continue
            attempt_started = time.perf_counter()
            repair = exact_q2_local_repair(
                initial,
                data,
                neighborhood,
                cache=cache,
                config=config,
                require_primary_improvement=False,
                allowed_primary_deterioration_minutes=args.allowed_deterioration,
                prioritize_four_stop=True,
                selection_seed=args.seed * 1000 + source * 10 + size,
                maximum_repaired_routes=size - 1,
                search_context={
                    "schema_version": 2,
                    "candidate_source": "TARGETED_6_ROUTE" if size == 6 else "TARGETED_5_ROUTE" if size == 5 else "ABSORPTION",
                    "lineage_id": args.lineage_id,
                    "warm_start_objective": initial.metrics.total_aircraft_time_minutes,
                    "warm_start_flights": initial.metrics.total_flights,
                    "basin_fingerprint": q2_basin_fingerprint(initial),
                    "source_route": source,
                    "source_absorption_score": source_row["absorption_score"],
                    "source_route_time": source_row["aircraft_time"],
                    "source_passengers": source_row["passenger_count"],
                    "source_utilization": source_row["utilization"],
                    "source_LAND_fraction": source_row["land_fraction"],
                    "target_count": size - 1,
                    "predicted_absorption_score": source_row["absorption_score"],
                    "predicted_flight_delta": 1,
                },
            )
            candidate = repair.solution
            queued = queue.add_from_repair(repair)
            flight_delta = (
                initial.metrics.total_flights - candidate.metrics.total_flights
                if candidate is not None
                else 0
            )
            accepted_best = bool(candidate is not None and _key(candidate) < _key(best))
            accepted95 = bool(
                candidate is not None
                and candidate.metrics.total_flights <= initial.metrics.total_flights - 1
                and (best95 is None or _key(candidate) < _key(best95))
            )
            if accepted_best:
                best = candidate
            if accepted95:
                best95 = candidate
                _materialize(
                    best95,
                    run_dir / "best-95-checkpoints" / f"source-{source:03d}-n{size}",
                    data,
                )
            for row_index, row in enumerate(repair.diagnostics.get("candidate_log", [])):
                payload = {
                    **row,
                    "run_id": run_id,
                    "seed": args.seed,
                    "iteration": len(attempts),
                    "destroy_operator": "flight_elimination",
                    "destroy_size": size,
                    "source_routes": list(neighborhood),
                    "repair_feasible": candidate is not None,
                    "repair_accepted": accepted_best,
                    "primary_gain": (
                        initial.metrics.total_aircraft_time_minutes
                        - candidate.metrics.total_aircraft_time_minutes
                        if accepted_best and candidate is not None
                        else 0
                    ),
                    "secondary_gain": 0,
                    "new_global_best": accepted_best,
                    "flight_elimination": flight_delta > 0,
                }
                identity = repr(
                    (
                        run_id,
                        source,
                        size,
                        row_index,
                        payload.get("candidate_sequence"),
                        payload.get("candidate_variant"),
                    )
                ).encode("utf-8")
                payload["candidate_id"] = hashlib.sha256(identity).hexdigest()[:24]
                candidate_stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
            attempts.append(
                {
                    "source_rank": source_slot + 1,
                    "source_route": source,
                    "neighborhood_size": size,
                    "neighborhood": json.dumps(neighborhood),
                    "candidate_sequences": repair.diagnostics.get("candidate_sequences", 0),
                    "candidate_variants": repair.diagnostics.get("candidate_variants", 0),
                    "compatible_assignments": repair.diagnostics.get("compatible_assignments", 0),
                    "status": repair.diagnostics.get("primary_status"),
                    "restricted_bound": repair.diagnostics.get("primary_dual_bound"),
                    "restricted_gap": repair.diagnostics.get("primary_mip_gap"),
                    "repair_success": candidate is not None,
                    "after_aircraft": candidate.metrics.total_aircraft_time_minutes if candidate else None,
                    "after_flights": candidate.metrics.total_flights if candidate else None,
                    "flight_delta": flight_delta,
                    "global_new_best": accepted_best,
                    "best95": accepted95,
                    "queued": queued,
                    "runtime_seconds": round(time.perf_counter() - attempt_started, 6),
                }
            )
            with (run_dir / "absorption-attempts.jsonl").open(
                "a", encoding="utf-8"
            ) as attempt_stream:
                attempt_stream.write(
                    json.dumps(attempts[-1], separators=(",", ":")) + "\n"
                )
            candidate_stream.flush()
    candidate_stream.close()
    queue.save(run_dir / "promising-master-queue.json")

    deep_rows: list[dict[str, object]] = []
    for entry in queue.entries[:6]:
        deep_started = time.perf_counter()
        repair = deepen_promising_master(
            initial,
            data,
            entry,
            cache=cache,
            base_config=config,
            candidate_budget=args.candidate_budget + 8,
            primary_seconds=args.deep_primary_time,
        )
        candidate = repair.solution
        accepted = bool(candidate is not None and _key(candidate) < _key(best))
        if accepted:
            best = candidate
        deep_rows.append(
            {
                "identity": entry.identity,
                "source_routes": json.dumps(entry.source_routes),
                "initial_incumbent": entry.initial_incumbent,
                "initial_bound": entry.initial_bound,
                "initial_gap": entry.initial_gap,
                "deep_incumbent": candidate.metrics.total_aircraft_time_minutes if candidate else None,
                "deep_bound": repair.diagnostics.get("primary_dual_bound"),
                "deep_gap": repair.diagnostics.get("primary_mip_gap"),
                "extra_compute_seconds": round(time.perf_counter() - deep_started, 6),
                "global_new_best": accepted,
            }
        )

    final_metrics = _materialize(best, run_dir / "best", data)
    best95_metrics = None
    if best95 is not None:
        best95_metrics = _materialize(best95, run_dir / "best-95-flight", data)
    write_csv(run_dir / "absorption-attempts.csv", tuple(attempts[0]) if attempts else (), attempts)
    write_csv(run_dir / "deep-resolve.csv", tuple(deep_rows[0]) if deep_rows else (), deep_rows)
    write_json(
        run_dir / "run_config.json",
        {
            "run_id": run_id,
            "git_commit": _git_value("rev-parse", "HEAD"),
            "git_branch": _git_value("branch", "--show-current"),
            "git_dirty": bool(_git_value("status", "--porcelain")),
            "runtime": {"python": platform.python_version(), "scipy": scipy.__version__, "backend": "scipy.optimize.milp/HiGHS"},
            "source": {"directory": str(args.start_dir.resolve()), "routes_sha256": sha256(args.start_dir / "q2-routes.csv"), "assignments_sha256": sha256(args.start_dir / "q2-assignments.csv"), "metrics": initial.metrics.to_dict()},
            "config": {
                key: str(value) if isinstance(value, Path) else value
                for key, value in vars(args).items()
            },
            "lineage_id": args.lineage_id,
            "bound_scope": "restricted_local_master",
            "cache": cache.stats(),
            "attempt_count": len(attempts),
            "deep_resolve_count": len(deep_rows),
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        },
    )
    summary = {
        "initial": initial.metrics.to_dict(),
        "best": final_metrics,
        "best95": best95_metrics,
        "lowest_flights": best95_metrics["total_flights"] if best95_metrics else initial.metrics.total_flights,
        "attempts": len(attempts),
        "promising_queued": len(queue.entries),
        "deep_resolves": len(deep_rows),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }
    write_json(run_dir / "absorption-summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
