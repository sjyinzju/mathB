from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_csv, write_json
from src.solver import load_problem_data
from src.solver.q3 import (
    export_q3_schedule,
    load_q3_people,
    load_q3_schedule,
    load_q3_variants,
    optimize_fixed_flight_assignments,
    project_mandatory_only,
    schedule_metrics,
    stage1_key,
    stage2_key,
)
from src.solver.q3_bounds import (
    candidate_route_master_lp_bound,
    layered_multicommodity_flow_bound,
)
from src.solver.q3_closure_p2 import optional_feasibility_dossiers, route_cache_provenance
from src.solver.q3_pro import (
    build_route_library,
    exact_fix_and_optimize,
    long_horizon_alns,
    optimize_stage2_under_cap,
    path_relink_and_recombine,
    preprocess_neighborhoods,
    solution_signature,
)
from src.validation import validate_solution


def validate_export(name, flights, people, data, directory: Path):
    routes = directory / f"{name}-routes.csv"
    assignments = directory / f"{name}-assignments.csv"
    export_q3_schedule(flights, people, routes, assignments, data.config)
    result = validate_solution(
        "q3", routes, assignments, data_dir=ROOT / "data/raw", config=data.config
    )
    write_json(directory / f"{name}-validator.json", result.to_dict())
    if not result.valid or result.metrics is None:
        raise RuntimeError(
            f"{name} failed independent Validator: "
            + "; ".join(str(issue) for issue in result.issues[:10])
        )
    memory = schedule_metrics(flights, people)
    exported = result.metrics.to_dict()
    for key in (
        "total_aircraft_time_minutes",
        "total_passenger_travel_time_minutes",
        "total_flights",
        "total_fuel_consumption_kg",
        "seat_utilization",
    ):
        if abs(float(memory[key]) - float(exported[key])) > 1e-6:
            raise RuntimeError(
                f"{name} memory/export mismatch for {key}: "
                f"{memory[key]} != {exported[key]}"
            )
    return result, routes, assignments


def restricted_pool(variants_by_od, incumbent, *, per_od: int = 3):
    used = {flight.variant.key for flight in incumbent}
    result = {}
    for od, variants in variants_by_od.items():
        values = sorted(
            variants,
            key=lambda variant: (
                variant.duration / max(1, variant.capacity),
                variant.duration,
                variant.fuel_kg,
                variant.key,
            ),
        )
        selected = [variant for variant in values if variant.key in used]
        selected.extend(values[:per_od])
        result[od] = tuple({variant.key: variant for variant in selected}.values())
    return result


def robustness_checks(stage1, people, data):
    by_aircraft = defaultdict(list)
    for flight in stage1:
        by_aircraft[flight.aircraft_id].append(flight)
    turnaround = {}
    for value in (40, 45):
        violations = []
        for aircraft_id, flights in by_aircraft.items():
            ordered = sorted(flights, key=lambda flight: flight.start)
            for left, right in zip(ordered, ordered[1:]):
                gap = right.start - left.end
                if gap < value:
                    violations.append(
                        {
                            "aircraft_id": aircraft_id,
                            "left_end": left.end,
                            "right_start": right.start,
                            "gap": gap,
                        }
                    )
        turnaround[str(value)] = {
            "incumbent_remains_feasible_without_recovery": not violations,
            "violating_connections": len(violations),
            "examples": violations[:10],
        }
    aircraft_minutes = Counter()
    for flight in stage1:
        aircraft_minutes[flight.aircraft_id] += flight.duration
    unavailable = []
    for aircraft_id, minutes in aircraft_minutes.most_common(3):
        remaining = [deepcopy(flight) for flight in stage1 if flight.aircraft_id != aircraft_id]
        try:
            repaired, _unserved, stats = optimize_fixed_flight_assignments(
                remaining, people, data.config, time_limit_seconds=10.0
            )
            metrics = schedule_metrics(repaired, people)
            feasible = int(metrics["served_mandatory"]) == len(people)
            status = stats
        except RuntimeError as exc:
            feasible = False
            metrics = None
            status = {"error": str(exc)}
        unavailable.append(
            {
                "aircraft_id": aircraft_id,
                "removed_minutes": minutes,
                "fixed-route_recovery_feasible": feasible,
                "metrics": metrics,
                "status": status,
            }
        )
    return {
        "scope": "fast deterministic incumbent stress tests; no claim of globally optimal recovery",
        "turnaround": turnaround,
        "single_aircraft_unavailable": unavailable,
    }


def repo_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Q3 PRO long-horizon optimizer")
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs/q3/q3-pro")
    parser.add_argument("--source-best", type=Path, default=ROOT / "outputs/q3/best")
    parser.add_argument("--diverse-seed", type=Path, default=ROOT / "outputs/q3/p0_p1_best")
    parser.add_argument("--variant-cache", type=Path, default=ROOT / "outputs/q2/pair_n3_h10.pkl")
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--wall-time", type=float, default=7200.0)
    parser.add_argument("--restart-threshold", type=int, default=80)
    parser.add_argument("--master-seed", type=int, default=20260816)
    parser.add_argument("--assignment-milp-time", type=float, default=20.0)
    parser.add_argument("--exact-windows", type=int, default=24)
    parser.add_argument("--path-pairs", type=int, default=12)
    parser.add_argument("--stage2-trials", type=int, default=80)
    parser.add_argument("--run-global-bound", action="store_true")
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    if args.iterations < 1 or args.exact_windows < 0:
        raise ValueError("iterations must be positive and exact windows nonnegative")
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q3-pro"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(run_dir)
    for child in (
        run_dir,
        args.output_root / "elite_pool",
        args.output_root / "route_library",
        args.output_root / "reports",
        args.output_root / "logs",
    ):
        child.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    mandatory = {pid: person for pid, person in people.items() if person.mandatory}
    optional_count = len(people) - len(mandatory)
    variants = load_q3_variants(args.variant_cache, people.values(), data.config)
    unique_variants = len({variant.key for values in variants.values() for variant in values})
    cache_provenance = route_cache_provenance(args.variant_cache, unique_variants)
    write_json(run_dir / "route_cache_state.json", cache_provenance)

    baseline_stage1 = load_q3_schedule(
        args.source_best / "q3-base-routes.csv",
        args.source_best / "q3-base-assignments.csv",
        people,
        variants,
        data.config,
    )
    baseline_stage2 = load_q3_schedule(
        args.source_best / "q3-routes.csv",
        args.source_best / "q3-assignments.csv",
        people,
        variants,
        data.config,
    )
    baseline_s1_result, _, _ = validate_export(
        "baseline-stage1", baseline_stage1, people, data, run_dir
    )
    baseline_s2_result, _, _ = validate_export(
        "baseline-stage2", baseline_stage2, people, data, run_dir
    )
    baseline_s1 = baseline_s1_result.metrics.to_dict()
    baseline_s2 = baseline_s2_result.metrics.to_dict()

    filtered_variants, feasibility_cache, preprocessing = preprocess_neighborhoods(
        people, variants, data
    )
    write_json(run_dir / "preprocessing.json", preprocessing)

    seeds = [("canonical-29659", baseline_stage1)]
    if args.diverse_seed.exists():
        diverse = load_q3_schedule(
            args.diverse_seed / "q3-base-routes.csv",
            args.diverse_seed / "q3-base-assignments.csv",
            people,
            variants,
            data.config,
        )
        validate_export("diverse-seed", diverse, people, data, run_dir)
        seeds.append(("p0-p1-diverse-30180", diverse))

    alns_best, elite_pool, alns = long_horizon_alns(
        seeds,
        mandatory,
        filtered_variants,
        data,
        iterations=args.iterations,
        wall_time_seconds=args.wall_time,
        seed=args.master_seed,
        restart_threshold=args.restart_threshold,
        assignment_time_limit_seconds=args.assignment_milp_time,
        elite_size=30,
    )
    write_json(run_dir / "alns-trace.json", alns)
    convergence = alns.pop("convergence")
    write_csv(
        run_dir / "convergence.csv",
        list(convergence[0]) if convergence else ["iteration"],
        convergence,
    )

    relinked, relinking = path_relink_and_recombine(
        elite_pool,
        mandatory,
        data,
        maximum_pairs=args.path_pairs,
        assignment_time_limit_seconds=args.assignment_milp_time,
    )
    write_json(run_dir / "path-relinking.json", relinking)

    pre_exact = min(
        (baseline_stage1, alns_best, relinked),
        key=lambda flights: stage1_key(flights, mandatory),
    )
    exact_best, exact_trace = exact_fix_and_optimize(
        pre_exact,
        mandatory,
        filtered_variants,
        data,
        windows=args.exact_windows,
        seed=args.master_seed,
        assignment_time_limit_seconds=args.assignment_milp_time,
    )
    write_json(run_dir / "exact-lns-trace.json", exact_trace)
    stage1 = min(
        (baseline_stage1, alns_best, relinked, exact_best),
        key=lambda flights: stage1_key(flights, mandatory),
    )

    feedback_rows = []
    stage2 = baseline_stage2
    for feedback_round in range(1, 4):
        stage2, stage2_trace = optimize_stage2_under_cap(
            stage1,
            stage2,
            people,
            filtered_variants,
            data,
            trials=args.stage2_trials,
            assignment_time_limit_seconds=args.assignment_milp_time,
        )
        projected = project_mandatory_only(stage2, people)
        improves = stage1_key(projected, mandatory) < stage1_key(stage1, mandatory)
        feedback_rows.append(
            {
                "round": feedback_round,
                "stage1": schedule_metrics(stage1, mandatory),
                "stage2": schedule_metrics(stage2, people),
                "projection": schedule_metrics(projected, mandatory),
                "projection_improves_stage1": improves,
                "stage2_trace": stage2_trace,
            }
        )
        if not improves:
            break
        stage1 = projected
    write_json(run_dir / "final-feedback.json", feedback_rows)

    # Objective fixing / secondary polish on the final structures.
    polished_stage1, missing, stage1_assignment = optimize_fixed_flight_assignments(
        stage1, mandatory, data.config, time_limit_seconds=args.assignment_milp_time
    )
    if missing:
        raise RuntimeError(f"secondary Stage 1 polish lost mandatory people: {missing[:8]}")
    if stage1_key(polished_stage1, mandatory) < stage1_key(stage1, mandatory):
        stage1 = polished_stage1
    cap = int(schedule_metrics(stage1, mandatory)["total_aircraft_time_minutes"])
    if int(schedule_metrics(stage2, people)["total_aircraft_time_minutes"]) > cap:
        stage2, _trace = optimize_stage2_under_cap(
            stage1,
            stage2,
            people,
            filtered_variants,
            data,
            trials=args.stage2_trials,
            assignment_time_limit_seconds=args.assignment_milp_time,
        )
    polished_stage2, unserved, stage2_assignment = optimize_fixed_flight_assignments(
        stage2, people, data.config, time_limit_seconds=args.assignment_milp_time
    )
    if stage2_key(polished_stage2, people) < stage2_key(stage2, people):
        stage2 = polished_stage2

    stage1_result, stage1_routes, stage1_assignments = validate_export(
        "q3-pro-base", stage1, people, data, run_dir
    )
    stage2_result, stage2_routes, stage2_assignments = validate_export(
        "q3-pro-final", stage2, people, data, run_dir
    )
    final_s1 = stage1_result.metrics.to_dict()
    final_s2 = stage2_result.metrics.to_dict()
    served_optional = optional_count - final_s2["unserved_optional_passengers"]
    if final_s1["total_aircraft_time_minutes"] > baseline_s1["total_aircraft_time_minutes"]:
        raise RuntimeError("Q3 PRO Stage 1 regressed against canonical baseline")
    if final_s2["total_aircraft_time_minutes"] > final_s1["total_aircraft_time_minutes"]:
        raise RuntimeError("Q3 PRO Stage 2 exceeds the final Stage 1 cap")

    shutil.copy2(stage1_routes, run_dir / "q3-base-routes.csv")
    shutil.copy2(stage1_assignments, run_dir / "q3-base-assignments.csv")
    shutil.copy2(stage2_routes, run_dir / "q3-routes.csv")
    shutil.copy2(stage2_assignments, run_dir / "q3-assignments.csv")
    shutil.copy2(run_dir / "q3-pro-base-validator.json", run_dir / "q3-base-validator.json")
    shutil.copy2(run_dir / "q3-pro-final-validator.json", run_dir / "q3-validator.json")

    for record in elite_pool.records:
        if record.signature == solution_signature(stage1):
            record.validator_status = "independent_validator_zero_issues"
    write_json(run_dir / "elite-pool.json", elite_pool.summary())
    route_library = build_route_library(
        filtered_variants,
        stage1,
        source=f"q3-pro:{run_id}",
        path=args.output_root / "route_library" / "routes.json",
    )
    write_json(run_dir / "route-library-state.json", route_library)

    p3_started = time.perf_counter()
    restricted = restricted_pool(filtered_variants, stage1, per_od=3)
    restricted_bound = candidate_route_master_lp_bound(
        mandatory.values(), restricted, data, time_limit_seconds=300.0
    )
    full_bound = candidate_route_master_lp_bound(
        mandatory.values(), filtered_variants, data, time_limit_seconds=300.0
    )
    if args.run_global_bound:
        global_bound = layered_multicommodity_flow_bound(
            mandatory.values(), data, time_limit_seconds=600.0
        )
        global_bound_dict = global_bound.to_dict()
        global_lb = global_bound.objective_minutes_integer_ceiling
    else:
        global_bound_dict = {
            "name": "carried_forward_validated_global_bound",
            "objective_minutes_integer_ceiling": 14125,
            "valid_for_original_problem": True,
            "not_recomputed_this_run": True,
        }
        global_lb = 14125
    p3 = {
        "restricted_master": restricted_bound.to_dict(),
        "priced_full_pool_master": full_bound.to_dict(),
        "pricing_columns_added": len(
            {v.key for values in filtered_variants.values() for v in values}
            - {v.key for values in restricted.values() for v in values}
        ),
        "restricted_objective": restricted_bound.objective_minutes_continuous,
        "post_pricing_objective": full_bound.objective_minutes_continuous,
        "negative_reduced_cost_effect": max(
            0.0,
            restricted_bound.objective_minutes_continuous
            - full_bound.objective_minutes_continuous,
        ),
        "global_bound": global_bound_dict,
        "runtime_seconds": round(time.perf_counter() - p3_started, 6),
        "interpretation": "RMP/dual/pricing bounds are restricted-pool diagnostics unless explicitly marked globally valid.",
    }
    write_json(run_dir / "p3-rmp-pricing.json", p3)

    gap = round(
        100.0 * (final_s1["total_aircraft_time_minutes"] - global_lb)
        / final_s1["total_aircraft_time_minutes"],
        6,
    )
    bounds = {
        "stage1": {
            "incumbent_upper_bound_minutes": final_s1["total_aircraft_time_minutes"],
            "enhanced_global_lower_bound_minutes": global_lb,
            "certified_gap_percent": gap,
            "restricted_master_lp_minutes": restricted_bound.objective_minutes_continuous,
            "priced_full_pool_lp_minutes": full_bound.objective_minutes_continuous,
            "candidate_pool_reference_is_global_bound": False,
        },
        "stage2": {
            "served_optional_incumbent": served_optional,
            "optional_upper_bound": optional_count,
            "absolute_gap": optional_count - served_optional,
            "proven_optimal_for_original_problem": served_optional == optional_count,
            "fixed_flight_assignment_optimal_only": bool(stage2_assignment["fixed_flight_optimal"]),
            "unserved_ids": unserved,
        },
    }
    write_json(run_dir / "bounds.json", bounds)
    robustness = robustness_checks(stage1, mandatory, data)
    write_json(run_dir / "robustness.json", robustness)

    metrics = {
        "baseline_stage1": baseline_s1,
        "baseline_stage2": baseline_s2,
        "final_stage1": final_s1,
        "final_stage2": final_s2,
        "served_optional": served_optional,
        "unserved_optional_ids": unserved,
        "stage1_assignment": stage1_assignment,
        "stage2_assignment": stage2_assignment,
        "runtime_seconds": round(time.perf_counter() - started, 6),
    }
    write_json(run_dir / "metrics.json", metrics)
    write_json(
        run_dir / "run_config.json",
        {
            **{
                key: str(value) if isinstance(value, Path) else value
                for key, value in vars(args).items()
            },
            "run_id": run_id,
            "source_commit": repo_sha(),
            "route_cache": cache_provenance,
            "total_elapsed_seconds": round(time.perf_counter() - started, 6),
        },
    )

    improvement = baseline_s1["total_aircraft_time_minutes"] - final_s1["total_aircraft_time_minutes"]
    report = f"""# Q3 PRO Final Report

## Repository

- Source `origin/platinumist_update`: `e0041235f5d11f36e0af1a6f4f680a8b5f6d6b57`
- Q3 PRO run commit: `{repo_sha()}`
- Run: `{run_id}`

## Final Stage 1

| Mandatory | Aircraft min | Passenger min | Flights | Fuel kg | Utilization | Validator |
|---:|---:|---:|---:|---:|---:|---|
| 3840/3840 | {final_s1['total_aircraft_time_minutes']} | {final_s1['total_passenger_travel_time_minutes']} | {final_s1['total_flights']} | {final_s1['total_fuel_consumption_kg']} | {final_s1['seat_utilization']:.6f} | 0 issues |

## Final Stage 2

| Cap | Mandatory | Temporary | Aircraft min | Passenger min | Flights | Fuel kg | Utilization | Validator |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| {final_s1['total_aircraft_time_minutes']} | 3840/3840 | {served_optional}/{optional_count} | {final_s2['total_aircraft_time_minutes']} | {final_s2['total_passenger_travel_time_minutes']} | {final_s2['total_flights']} | {final_s2['total_fuel_consumption_kg']} | {final_s2['seat_utilization']:.6f} | 0 issues |

Unserved temporary IDs: `{', '.join(unserved) if unserved else 'none'}`.

## Improvement History

- Trusted baseline Stage 1: {baseline_s1['total_aircraft_time_minutes']} min.
- Q3 PRO final Stage 1: {final_s1['total_aircraft_time_minutes']} min.
- Net Q3 PRO improvement: {improvement} min.
- Long ALNS iterations: {alns['completed_iterations']}; global improvements: {alns['global_improvements']}; restarts: {alns['restarts']}.
- Exact/fix-and-optimize windows: {args.exact_windows}.
- Path-relink attempts: {relinking['attempts']}.

## Route Library

- Deduplicated routes: {route_library['route_count']}.
- Routes used by final Stage 1: {route_library['used_by_final']}.
- Q2 cache remained read-only; SHA-256 `{cache_provenance['sha256']}`.

## Bounds and exact status

- Globally valid lower bound: {global_lb} min.
- Best feasible UB: {final_s1['total_aircraft_time_minutes']} min.
- Certified gap: {gap}%.
- Restricted-master LP: {restricted_bound.objective_minutes_continuous:.6f} min.
- Full finite-pool LP after batch pricing: {full_bound.objective_minutes_continuous:.6f} min.
- The finite-pool LP is not a global bound.
- Stage 2 fixed-flight assignment status: `{stage2_assignment['stage1_message']}`; this proves only the fixed final structure.
- No global 159/160 infeasibility claim is made unless all 160 are served.

## Reproducibility

```powershell
cd code
python scripts/12_run_q3_pro.py --run-id {run_id} --iterations {args.iterations} --wall-time {args.wall_time} --restart-threshold {args.restart_threshold} --master-seed {args.master_seed} --exact-windows {args.exact_windows} --stage2-trials {args.stage2_trials}{' --run-global-bound' if args.run_global_bound else ''}
```

Detailed convergence, operator, failure, pricing, elite, feedback, exact-LNS and robustness artifacts are in `{run_dir}`.

## Remaining limitations

- The global Stage 1 gap remains wide because the strongest global relaxation drops several routing and scheduling integrality features.
- Stage 2 optimality is certified only for the fixed final flight structure; unrestricted 159/160 feasibility remains open when the incumbent is below 160.
- Lagrangian/Benders are retained as research directions because the higher-priority ALNS, exact LNS, recombination and pricing pipeline consumed the useful search budget.
"""
    report_path = args.output_root / "reports" / "Q3_PRO_FINAL_REPORT.md"
    report_path.write_text(report, encoding="utf-8")
    (run_dir / "Q3_PRO_FINAL_REPORT.md").write_text(report, encoding="utf-8")

    current = args.output_root / "current_incumbent"
    current.mkdir(parents=True, exist_ok=True)
    for name in (
        "q3-base-routes.csv",
        "q3-base-assignments.csv",
        "q3-routes.csv",
        "q3-assignments.csv",
        "q3-base-validator.json",
        "q3-validator.json",
        "metrics.json",
        "bounds.json",
        "run_config.json",
        "Q3_PRO_FINAL_REPORT.md",
    ):
        shutil.copy2(run_dir / name, current / name)

    if args.promote:
        destination = ROOT / "outputs/q3/best"
        for name in (
            "q3-base-routes.csv",
            "q3-base-assignments.csv",
            "q3-routes.csv",
            "q3-assignments.csv",
            "q3-base-validator.json",
            "q3-validator.json",
            "metrics.json",
            "bounds.json",
            "Q3_PRO_FINAL_REPORT.md",
        ):
            shutil.copy2(run_dir / name, destination / name)

    print(
        f"Q3 PRO PASS: Stage1={final_s1['total_aircraft_time_minutes']}, "
        f"Stage2={served_optional}/{optional_count} @ {final_s2['total_aircraft_time_minutes']}, "
        f"run_dir={run_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
