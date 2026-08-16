from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_csv, write_json
from src.solver import load_problem_data
from src.solver.q3 import (
    build_flexibility_profiles,
    build_mandatory_schedule,
    destroy_repair_route_descent,
    export_q3_schedule,
    load_q3_people,
    load_q3_schedule,
    load_q3_variants,
    multiflight_ruin_recreate_descent,
    optimize_fixed_flight_assignments,
    project_mandatory_only,
    retype_and_rehome_flights,
    schedule_comparison_key,
    schedule_metrics,
    shorten_fixed_flight_routes,
)
from src.validation import validate_solution


MODES = (
    "priority",
    "deadline",
    "slack",
    "day_scarcity",
    "route_scarcity",
    "criticality",
    "od_density",
    "regret_proxy",
    "randomized_deadline",
    "randomized_criticality",
    "flexible_regret",
)


def _validate_export(
    name,
    flights,
    people,
    data,
    run_dir: Path,
):
    routes = run_dir / f"{name}-routes.csv"
    assignments = run_dir / f"{name}-assignments.csv"
    export_q3_schedule(flights, people, routes, assignments, data.config)
    result = validate_solution(
        "q3",
        routes,
        assignments,
        data_dir=ROOT / "data/raw",
        config=data.config,
    )
    write_json(run_dir / f"{name}-validator.json", result.to_dict())
    if not result.valid:
        raise RuntimeError(
            f"{name} failed independent validator: "
            + "; ".join(str(issue) for issue in result.issues[:8])
        )
    return result, routes, assignments


def _polish_stage1(
    flights,
    mandatory_people,
    variants,
    data,
    args,
):
    started = time.perf_counter()
    work = list(flights)
    trace: list[dict[str, object]] = []
    for round_index in range(args.polish_rounds):
        before = schedule_metrics(work, mandatory_people)
        work, _unserved, assignment = optimize_fixed_flight_assignments(
            work,
            mandatory_people,
            data.config,
            time_limit_seconds=args.assignment_milp_time,
        )
        work, shortening = shorten_fixed_flight_routes(
            work, mandatory_people, variants, data.config
        )
        work, retype = retype_and_rehome_flights(
            work,
            mandatory_people,
            variants,
            data.config,
            maximum_passes=2,
        )
        after = schedule_metrics(work, mandatory_people)
        trace.append(
            {
                "round": round_index + 1,
                "before": before,
                "after": after,
                "assignment": assignment,
                "shortening": shortening,
                "retype_rehome": retype,
            }
        )
        if after["total_aircraft_time_minutes"] == before["total_aircraft_time_minutes"]:
            break

    work, descent = destroy_repair_route_descent(
        work,
        mandatory_people,
        variants,
        data.config,
        minimum_optional_served=0,
        maximum_trials=args.stage1_destroy_trials,
        assignment_time_limit_seconds=args.assignment_milp_time,
    )
    if args.multiflight_rr:
        work, multiflight = multiflight_ruin_recreate_descent(
            work,
            mandatory_people,
            variants,
            data.config,
            minimum_optional_served=0,
            maximum_trials=args.rr_trials,
            maximum_neighbors=args.rr_neighbors,
            route_limit=args.rr_route_limit,
            assignment_time_limit_seconds=args.rr_milp_time,
        )
    else:
        multiflight = {"enabled": False}
    work, _unserved, assignment = optimize_fixed_flight_assignments(
        work,
        mandatory_people,
        data.config,
        time_limit_seconds=args.assignment_milp_time,
    )
    work = [flight for flight in work if flight.person_ids]
    return work, {
        "rounds": trace,
        "destroy_repair": descent,
        "multiflight_rr": multiflight,
        "final_assignment": assignment,
        "runtime_seconds": round(time.perf_counter() - started, 6),
    }


def _stage2_candidates(stage1, source_final, people, variants, data, args):
    cap = int(schedule_metrics(stage1, people)["total_aircraft_time_minutes"])
    candidates = []
    source_metrics = schedule_metrics(source_final, people)
    if int(source_metrics["total_aircraft_time_minutes"]) <= cap:
        candidates.append(("previous_stage2", source_final, {"warm_start": True}))
    fixed, unserved, assignment = optimize_fixed_flight_assignments(
        stage1,
        people,
        data.config,
        time_limit_seconds=args.assignment_milp_time,
    )
    candidates.append(
        (
            "fixed_stage1_assignment",
            fixed,
            {"assignment": assignment, "unserved_optional": unserved},
        )
    )
    feasible = [
        item
        for item in candidates
        if int(schedule_metrics(item[1], people)["total_aircraft_time_minutes"]) <= cap
    ]
    name, best, details = min(
        feasible, key=lambda item: schedule_comparison_key(item[1], people, stage=2)
    )
    if args.multiflight_rr:
        served = int(schedule_metrics(best, people)["served_optional"])
        trial, rr = multiflight_ruin_recreate_descent(
            best,
            people,
            variants,
            data.config,
            minimum_optional_served=served,
            maximum_trials=args.rr_trials,
            maximum_neighbors=args.rr_neighbors,
            route_limit=args.rr_route_limit,
            assignment_time_limit_seconds=args.rr_milp_time,
        )
        if (
            int(schedule_metrics(trial, people)["total_aircraft_time_minutes"]) <= cap
            and schedule_comparison_key(trial, people, stage=2)
            < schedule_comparison_key(best, people, stage=2)
        ):
            best = trial
            name = name + "+multiflight_rr"
        details = {**details, "multiflight_rr": rr}
    return best, {"selected": name, "cap": cap, "details": details}


def main() -> int:
    parser = argparse.ArgumentParser(description="Q3 P0/P1反馈闭环与结构邻域优化")
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs/q3")
    parser.add_argument("--source-best", type=Path, default=ROOT / "outputs/q3/best")
    parser.add_argument(
        "--variant-cache", type=Path, default=ROOT / "outputs/q2/pair_n3_h10.pkl"
    )
    parser.add_argument("--feedback-rounds", type=int, default=2)
    parser.add_argument("--start-count", type=int, default=0)
    parser.add_argument("--deep-top-k", type=int, default=3)
    parser.add_argument("--polish-rounds", type=int, default=2)
    parser.add_argument("--stage1-destroy-trials", type=int, default=40)
    parser.add_argument("--assignment-milp-time", type=float, default=60.0)
    parser.add_argument("--multiflight-rr", action="store_true")
    parser.add_argument("--rr-trials", type=int, default=50)
    parser.add_argument("--rr-neighbors", type=int, default=8)
    parser.add_argument("--rr-route-limit", type=int, default=100)
    parser.add_argument("--rr-milp-time", type=float, default=20.0)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q3-p0-p1"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()

    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    mandatory_people = {
        person_id: person for person_id, person in people.items() if person.mandatory
    }
    optional_count = sum(not person.mandatory for person in people.values())
    variants = load_q3_variants(args.variant_cache, people.values(), data.config)
    source_base = load_q3_schedule(
        args.source_best / "q3-base-routes.csv",
        args.source_best / "q3-base-assignments.csv",
        people,
        variants,
        data.config,
    )
    source_final = load_q3_schedule(
        args.source_best / "q3-routes.csv",
        args.source_best / "q3-assignments.csv",
        people,
        variants,
        data.config,
    )
    old_base_validation, _, _ = _validate_export(
        "audit-old-stage1", source_base, people, data, run_dir
    )
    old_final_validation, _, _ = _validate_export(
        "audit-old-stage2", source_final, people, data, run_dir
    )

    projected = project_mandatory_only(source_final, people)
    projected_validation, _, _ = _validate_export(
        "p0-projected-stage1", projected, people, data, run_dir
    )
    audit = {
        "old_stage1": old_base_validation.metrics.to_dict(),
        "old_stage2": old_final_validation.metrics.to_dict(),
        "projection": projected_validation.metrics.to_dict(),
        "stage2_validator_valid": old_final_validation.valid,
        "projection_validator_valid": projected_validation.valid,
        "projection_logically_applicable": True,
        "route_cache": {
            "path": str(args.variant_cache),
            "size_bytes": args.variant_cache.stat().st_size,
            "mtime": args.variant_cache.stat().st_mtime,
        },
    }
    write_json(run_dir / "p0_projection_audit.json", audit)

    candidates = [("old_stage1", source_base), ("stage2_projection", projected)]
    multistart_rows = []
    if args.start_count > 0:
        profiles = build_flexibility_profiles(
            mandatory_people.values(), variants, data.config
        )
        raw_candidates = []
        for index in range(args.start_count):
            mode = MODES[index % len(MODES)]
            construction_started = time.perf_counter()
            flights, stats = build_mandatory_schedule(
                people,
                variants,
                data,
                mode=mode,
                seed=index,
                flexibility_profiles=profiles,
            )
            metrics = schedule_metrics(flights, people)
            row = {
                "mode": mode,
                "seed": index,
                "feasible": stats.feasible,
                **metrics,
                "runtime_seconds": round(
                    time.perf_counter() - construction_started, 6
                ),
            }
            multistart_rows.append(row)
            if stats.feasible:
                raw_candidates.append((mode, flights))
        raw_candidates.sort(
            key=lambda item: schedule_comparison_key(item[1], people, stage=1)
        )
        selected_modes = set()
        top = []
        for mode, flights in raw_candidates:
            if len(top) >= args.deep_top_k:
                break
            if mode in selected_modes and len(raw_candidates) > args.deep_top_k:
                continue
            selected_modes.add(mode)
            top.append((mode, flights))
        for mode, flights in top:
            polished, trace = _polish_stage1(
                flights, mandatory_people, variants, data, args
            )
            candidates.append((f"multistart_{mode}", polished))
            write_json(run_dir / f"p1a-{mode}-trace.json", trace)
    if multistart_rows:
        write_csv(
            run_dir / "q3-multistart.csv",
            list(multistart_rows[0]),
            multistart_rows,
        )
        write_json(
            run_dir / "q3-multistart-summary.json",
            {
                "starts": len(multistart_rows),
                "feasible": sum(bool(row["feasible"]) for row in multistart_rows),
                "modes": sorted({str(row["mode"]) for row in multistart_rows}),
            },
        )

    stage1_name, stage1 = min(
        candidates,
        key=lambda item: schedule_comparison_key(item[1], people, stage=1),
    )
    feedback_trace = []
    stage2 = source_final
    for feedback_round in range(max(1, args.feedback_rounds)):
        before = schedule_metrics(stage1, people)
        polished, polish_trace = _polish_stage1(
            stage1, mandatory_people, variants, data, args
        )
        polished_validation, _, _ = _validate_export(
            f"feedback-{feedback_round + 1}-stage1-candidate",
            polished,
            people,
            data,
            run_dir,
        )
        if schedule_comparison_key(polished, people, stage=1) < schedule_comparison_key(
            stage1, people, stage=1
        ):
            stage1 = polished
            stage1_name += "+polish"
        stage2, stage2_trace = _stage2_candidates(
            stage1, source_final, people, variants, data, args
        )
        stage2_validation, _, _ = _validate_export(
            f"feedback-{feedback_round + 1}-stage2-candidate",
            stage2,
            people,
            data,
            run_dir,
        )
        projected_again = project_mandatory_only(stage2, people)
        projected_metrics = schedule_metrics(projected_again, people)
        improved_projection = int(projected_metrics["total_aircraft_time_minutes"]) < int(
            schedule_metrics(stage1, people)["total_aircraft_time_minutes"]
        )
        feedback_trace.append(
            {
                "round": feedback_round + 1,
                "before_stage1": before,
                "after_stage1": schedule_metrics(stage1, people),
                "stage1_validator": polished_validation.valid,
                "stage1_polish": polish_trace,
                "stage2": schedule_metrics(stage2, people),
                "stage2_validator": stage2_validation.valid,
                "stage2_trace": stage2_trace,
                "projection_strictly_improves_stage1": improved_projection,
            }
        )
        if not improved_projection:
            break
        stage1 = projected_again
        stage1_name = "stage2_feedback_projection"

    final_stage1_validation, stage1_routes, stage1_assignments = _validate_export(
        "q3-p0-p1-base", stage1, people, data, run_dir
    )
    final_stage2_validation, stage2_routes, stage2_assignments = _validate_export(
        "q3-p0-p1-final", stage2, people, data, run_dir
    )
    assert final_stage1_validation.metrics and final_stage2_validation.metrics
    stage1_metrics = final_stage1_validation.metrics.to_dict()
    stage2_metrics = final_stage2_validation.metrics.to_dict()
    if int(stage2_metrics["total_aircraft_time_minutes"]) > int(
        stage1_metrics["total_aircraft_time_minutes"]
    ):
        raise RuntimeError("P0/P1 final Stage 2 exceeds the updated Stage 1 cap")

    shutil.copy2(stage1_routes, run_dir / "q3-base-routes.csv")
    shutil.copy2(stage1_assignments, run_dir / "q3-base-assignments.csv")
    shutil.copy2(stage2_routes, run_dir / "q3-routes.csv")
    shutil.copy2(stage2_assignments, run_dir / "q3-assignments.csv")
    shutil.copy2(
        run_dir / "q3-p0-p1-base-validator.json", run_dir / "q3-base-validator.json"
    )
    shutil.copy2(
        run_dir / "q3-p0-p1-final-validator.json", run_dir / "q3-validator.json"
    )
    write_json(run_dir / "q3-p0-p1-feedback-trace.json", feedback_trace)

    enhanced_bound = 14125
    write_json(
        run_dir / "q3-bounds.json",
        {
            "stage1": {
                "incumbent_aircraft_time_minutes": stage1_metrics[
                    "total_aircraft_time_minutes"
                ],
                "enhanced_global_lower_bound_minutes": enhanced_bound,
                "conservative_gap_percent": round(
                    100
                    * (stage1_metrics["total_aircraft_time_minutes"] - enhanced_bound)
                    / stage1_metrics["total_aircraft_time_minutes"],
                    6,
                ),
            },
            "stage2": {
                "served_optional_incumbent": optional_count
                - stage2_metrics["unserved_optional_passengers"],
                "optional_upper_bound": optional_count,
                "proven_optimal": stage2_metrics["unserved_optional_passengers"] == 0,
                "aircraft_time_slack_minutes": stage1_metrics[
                    "total_aircraft_time_minutes"
                ]
                - stage2_metrics["total_aircraft_time_minutes"],
            },
        },
    )
    write_json(
        run_dir / "metrics.json",
        {
            "gate_pass": True,
            "selected_stage1_source": stage1_name,
            "mandatory_count": len(mandatory_people),
            "optional_count": optional_count,
            "served_optional": optional_count
            - stage2_metrics["unserved_optional_passengers"],
            "baseline_metrics": stage1_metrics,
            "final_metrics": stage2_metrics,
            "old_baseline_metrics": old_base_validation.metrics.to_dict(),
            "old_final_metrics": old_final_validation.metrics.to_dict(),
            "feedback_trace": feedback_trace,
        },
    )
    write_json(
        run_dir / "run_config.json",
        {
            **{
                key: str(value) if isinstance(value, Path) else value
                for key, value in vars(args).items()
            },
            "source_best": str(args.source_best),
            "variant_cache": str(args.variant_cache),
            "total_elapsed_seconds": round(time.perf_counter() - started, 6),
        },
    )
    old_optional_served = optional_count - int(
        old_final_validation.metrics.unserved_optional_passengers
    )
    result_markdown = f"""# Q3 P0–P1 Results

## Baseline

- Old Stage 1: {old_base_validation.metrics.total_aircraft_time_minutes} min.
- Old Stage 2: {old_final_validation.metrics.total_aircraft_time_minutes} min, temporary {old_optional_served}/{optional_count}.

## P0 projection and feedback

- The valid Stage 2 schedule was projected by deleting optional assignments only.
- New Stage 1: {stage1_metrics['total_aircraft_time_minutes']} min.
- Saved versus old Stage 1: {old_base_validation.metrics.total_aircraft_time_minutes - stage1_metrics['total_aircraft_time_minutes']} min.

## Final

- Stage 1: {stage1_metrics['total_aircraft_time_minutes']} min, {stage1_metrics['total_flights']} flights, validator 0 violations.
- Stage 2: {stage2_metrics['total_aircraft_time_minutes']} min, temporary {optional_count - stage2_metrics['unserved_optional_passengers']}/{optional_count}, validator 0 violations.
- Certified Stage 1 gap against 14125 min: {100 * (stage1_metrics['total_aircraft_time_minutes'] - enhanced_bound) / stage1_metrics['total_aircraft_time_minutes']:.3f}%.

## P1 components

- Multi-heuristic seed modes and Top-K interface are implemented.
- Hard-skeleton / flexibility / regret-priority mode is implemented.
- Same-day 2-to-1 multi-flight ruin-and-recreate is implemented.
- Time-aware offshore waiting scheduler and actual-timing export are implemented.

## Validation

All promoted CSV candidates were independently re-read and checked by the unchanged Q3 validator.
"""
    (run_dir / "Q3_P0_P1_RESULTS.md").write_text(result_markdown, encoding="utf-8")

    if args.promote:
        destination = args.output_root / "p0_p1_best"
        destination.mkdir(parents=True, exist_ok=True)
        for path in run_dir.iterdir():
            if path.is_file():
                shutil.copy2(path, destination / path.name)

    print(
        "Q3 P0/P1 PASS: "
        f"Stage1={stage1_metrics['total_aircraft_time_minutes']} min, "
        f"Stage2={stage2_metrics['total_aircraft_time_minutes']} min, "
        f"temporary={optional_count - stage2_metrics['unserved_optional_passengers']}/{optional_count}, "
        f"run_dir={run_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
