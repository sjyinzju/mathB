from __future__ import annotations

import argparse
import pickle
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_json
from src.solver import (
    Q2MasterConfig,
    adaptive_triple_sequences,
    build_q2_variant_pool,
    build_separate_q2_baseline,
    candidate_service_sequences,
    export_q1_solution,
    load_problem_data,
    load_q1_solution,
    load_q2_solution,
    solve_q2_master,
)
from src.validation import validate_solution


def _comparison_key(metrics: dict[str, object]) -> tuple[float, ...]:
    return (
        float(metrics["total_aircraft_time_minutes"]),
        float(metrics["total_passenger_travel_time_minutes"]),
        float(metrics["total_flights"]),
        float(metrics["total_fuel_consumption_kg"]),
        -float(metrics["seat_utilization"]),
    )


def _validate_and_write(solution, prefix: str, run_dir: Path, data):
    routes_path = run_dir / f"{prefix}-routes.csv"
    assignments_path = run_dir / f"{prefix}-assignments.csv"
    export_q1_solution(solution, routes_path, assignments_path)
    validation = validate_solution(
        "q2",
        routes_path,
        assignments_path,
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    write_json(run_dir / f"{prefix}-validator.json", validation.to_dict())
    return validation


def main() -> int:
    parser = argparse.ArgumentParser(description="问题二联合取送候选路线MILP求解器")
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "q2")
    parser.add_argument("--q1-routes", type=Path, default=ROOT / "outputs/q1/best/q1-routes.csv")
    parser.add_argument(
        "--q1-assignments",
        type=Path,
        default=ROOT / "outputs/q1/best/q1-assignments.csv",
    )
    parser.add_argument("--nearest-neighbors", type=int, default=3)
    parser.add_argument("--high-demand-nodes", type=int, default=10)
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument("--mip-gap", type=float, default=0.0)
    parser.add_argument("--variant-cache", type=Path)
    parser.add_argument("--initial-q2-routes", type=Path)
    parser.add_argument("--initial-q2-assignments", type=Path)
    parser.add_argument(
        "--triple-limit",
        type=int,
        default=0,
        help="在双设施主问题之后自适应增加的三设施候选序列数",
    )
    parser.add_argument("--skip-separate", action="store_true")
    parser.add_argument("--separate-only", action="store_true")
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q2-master"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"运行目录已存在：{run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()
    data = load_problem_data()
    q1 = load_q1_solution(args.q1_routes, args.q1_assignments, data)
    variant_started = time.perf_counter()
    if args.variant_cache and args.variant_cache.exists():
        with args.variant_cache.open("rb") as stream:
            cached = pickle.load(stream)
        sequences = tuple(cached["sequences"])
        variants = tuple(cached["variants"])
    else:
        sequences = candidate_service_sequences(
            data,
            seed_routes=q1.routes,
            nearest_neighbors=args.nearest_neighbors,
            high_demand_nodes=args.high_demand_nodes,
        )
        variants = build_q2_variant_pool(data, sequences)
        if args.variant_cache:
            args.variant_cache.parent.mkdir(parents=True, exist_ok=True)
            with args.variant_cache.open("wb") as stream:
                pickle.dump({"sequences": sequences, "variants": variants}, stream)
    variant_seconds = time.perf_counter() - variant_started
    master_config = Q2MasterConfig(
        nearest_neighbors=args.nearest_neighbors,
        high_demand_nodes=args.high_demand_nodes,
        time_limit_seconds=args.time_limit,
        mip_relative_gap=args.mip_gap,
    )

    separate = None
    separate_validation = None
    if not args.skip_separate:
        separate = build_separate_q2_baseline(data, variants, config=master_config)
        separate_validation = _validate_and_write(separate, "q2-separate", run_dir, data)
    if args.separate_only:
        if separate is None or separate_validation is None:
            raise ValueError("--separate-only cannot be combined with --skip-separate")
        metrics = separate_validation.metrics.to_dict() if separate_validation.metrics else None
        gate_pass = bool(
            separate_validation.valid
            and metrics
            and int(metrics["served_passengers"]) == data.q2_passenger_count
        )
        write_json(
            run_dir / "metrics.json",
            {
                "gate_pass": gate_pass,
                "validator_metrics": metrics,
                "separate_metrics": separate.metrics.to_dict(),
            },
        )
        write_json(
            run_dir / "run_config.json",
            {
                "method": separate.method,
                "candidate_sequences": len(sequences),
                "candidate_variants": len(variants),
                "variant_generation_seconds": round(variant_seconds, 6),
                "total_elapsed_seconds": round(time.perf_counter() - started, 6),
                "master_config": {
                    "nearest_neighbors": args.nearest_neighbors,
                    "high_demand_nodes": args.high_demand_nodes,
                    "time_limit_seconds": args.time_limit,
                    "mip_relative_gap": args.mip_gap,
                },
                "separate_diagnostics": separate.diagnostics,
            },
        )
        print(
            f"Q2 SEPARATE {'PASS' if gate_pass else 'FAIL'}: "
            f"time={metrics['total_aircraft_time_minutes'] if metrics else 'NA'} min"
        )
        return 0 if gate_pass else 2

    if bool(args.initial_q2_routes) != bool(args.initial_q2_assignments):
        raise ValueError(
            "--initial-q2-routes and --initial-q2-assignments must be provided together"
        )
    if args.initial_q2_routes:
        pair_solution = load_q2_solution(
            args.initial_q2_routes,
            args.initial_q2_assignments,
            data,
            method="q2_b1_imported_pair_route_master",
        )
    else:
        pair_solution = solve_q2_master(
            data,
            variants,
            config=master_config,
            method="q2_b1_joint_pair_route_master",
        )
    pair_validation = _validate_and_write(pair_solution, "q2-pair", run_dir, data)
    extra_sequences = ()
    extra_variants = ()
    joint = pair_solution
    expanded_solution = None
    if args.triple_limit > 0:
        extra_sequences = adaptive_triple_sequences(
            data,
            pair_solution.routes,
            limit=args.triple_limit,
        )
        extra_variants = build_q2_variant_pool(data, extra_sequences)
        merged = {variant.key: variant for variant in (*variants, *extra_variants)}
        expanded_solution = solve_q2_master(
            data,
            tuple(merged.values()),
            config=master_config,
            method="q2_b2_adaptive_triple_route_master",
        )
        if expanded_solution.metrics.comparison_key() < pair_solution.metrics.comparison_key():
            joint = expanded_solution
    joint_validation = _validate_and_write(joint, "q2", run_dir, data)
    validator_metrics = joint_validation.metrics.to_dict() if joint_validation.metrics else None
    internal_metrics = joint.metrics.to_dict()
    metrics_match = bool(
        validator_metrics
        and all(
            abs(float(validator_metrics[key]) - float(internal_metrics[key])) <= 1.0e-6
            for key in internal_metrics
        )
    )
    gate_pass = bool(
        joint_validation.valid
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
            "q1_skeleton_metrics": q1.metrics.to_dict(),
            "separate_metrics": separate.metrics.to_dict() if separate else None,
            "pair_master_metrics": pair_solution.metrics.to_dict(),
            "expanded_master_metrics": (
                expanded_solution.metrics.to_dict() if expanded_solution else None
            ),
            "joint_internal_metrics": internal_metrics,
            "validator_metrics": validator_metrics,
            "comparison_key": list(_comparison_key(validator_metrics)) if validator_metrics else None,
            "joint_improvement_from_separate": (
                {
                    "aircraft_time_minutes": (
                        separate.metrics.total_aircraft_time_minutes
                        - joint.metrics.total_aircraft_time_minutes
                    ),
                    "aircraft_time_percent": 100.0
                    * (
                        separate.metrics.total_aircraft_time_minutes
                        - joint.metrics.total_aircraft_time_minutes
                    )
                    / separate.metrics.total_aircraft_time_minutes,
                }
                if separate
                else None
            ),
        },
    )
    write_json(
        run_dir / "run_config.json",
        {
            "method": joint.method,
            "q1_routes": str(args.q1_routes),
            "candidate_sequences": len(sequences),
            "candidate_variants": len(variants),
            "adaptive_triple_sequences": len(extra_sequences),
            "adaptive_triple_variants": len(extra_variants),
            "variant_generation_seconds": round(variant_seconds, 6),
            "total_elapsed_seconds": round(elapsed, 6),
            "master_config": {
                "nearest_neighbors": args.nearest_neighbors,
                "high_demand_nodes": args.high_demand_nodes,
                "time_limit_seconds": args.time_limit,
                "mip_relative_gap": args.mip_gap,
            },
            "joint_diagnostics": joint.diagnostics,
            "pair_diagnostics": pair_solution.diagnostics,
            "pair_validator_valid": pair_validation.valid,
            "separate_diagnostics": separate.diagnostics if separate else None,
        },
    )
    if not gate_pass:
        print(f"Q2 GATE FAIL: {run_dir}", file=sys.stderr)
        return 2

    promoted = False
    if args.promote:
        best_dir = args.output_root / "best"
        previous_path = best_dir / "metrics.json"
        should_promote = True
        if previous_path.exists():
            import json

            previous = json.loads(previous_path.read_text(encoding="utf-8"))["validator_metrics"]
            should_promote = _comparison_key(validator_metrics) < _comparison_key(previous)
        if should_promote:
            best_dir.mkdir(parents=True, exist_ok=True)
            for path in run_dir.iterdir():
                if path.is_file():
                    shutil.copy2(path, best_dir / path.name)
            promoted = True
    print(
        f"Q2 PASS: time={validator_metrics['total_aircraft_time_minutes']} min, "
        f"passenger={validator_metrics['total_passenger_travel_time_minutes']} min, "
        f"flights={validator_metrics['total_flights']}, "
        f"fuel={validator_metrics['total_fuel_consumption_kg']} kg, "
        f"utilization={validator_metrics['seat_utilization']:.6f}, "
        f"elapsed={elapsed:.3f}s, promoted={promoted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
