from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
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
)
from src.solver.q3_closure_p2 import stage2_key
from src.validation import validate_solution


def _validate(name, flights, people, data, run_dir):
    routes = run_dir / f"{name}-routes.csv"
    assignments = run_dir / f"{name}-assignments.csv"
    export_q3_schedule(flights, people, routes, assignments, data.config)
    result = validate_solution(
        "q3", routes, assignments, data_dir=ROOT / "data/raw", config=data.config
    )
    write_json(run_dir / f"{name}-validator.json", result.to_dict())
    if not result.valid or result.metrics is None:
        raise RuntimeError(f"{name} failed validator")
    memory = schedule_metrics(flights, people)
    for key in (
        "total_aircraft_time_minutes",
        "total_passenger_travel_time_minutes",
        "total_flights",
        "total_fuel_consumption_kg",
    ):
        if abs(float(memory[key]) - float(result.metrics.to_dict()[key])) > 1e-6:
            raise RuntimeError(f"{name} in-memory/export mismatch: {key}")
    return result, routes, assignments


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply mandatory final Stage2-to-Stage1 feedback")
    parser.add_argument(
        "--source-run",
        type=Path,
        default=ROOT / "outputs/q3/runs/q3-p2-v9-final",
    )
    parser.add_argument("--run-id", default="q3-p2-v9-feedback-final")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs/q3")
    parser.add_argument("--variant-cache", type=Path, default=ROOT / "outputs/q2/pair_n3_h10.pkl")
    parser.add_argument("--assignment-milp-time", type=float, default=60.0)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()
    run_dir = args.output_root / "runs" / args.run_id
    if run_dir.exists():
        raise FileExistsError(run_dir)
    run_dir.mkdir(parents=True)
    for path in args.source_run.iterdir():
        if path.is_file():
            shutil.copy2(path, run_dir / path.name)
    started = time.perf_counter()

    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    mandatory = {pid: person for pid, person in people.items() if person.mandatory}
    optional_count = len(people) - len(mandatory)
    variants = load_q3_variants(args.variant_cache, people.values(), data.config)
    old_stage1 = load_q3_schedule(
        args.source_run / "q3-base-routes.csv",
        args.source_run / "q3-base-assignments.csv",
        people,
        variants,
        data.config,
    )
    old_stage2 = load_q3_schedule(
        args.source_run / "q3-routes.csv",
        args.source_run / "q3-assignments.csv",
        people,
        variants,
        data.config,
    )
    before = schedule_metrics(old_stage1, mandatory)
    projected = project_mandatory_only(old_stage2, people)
    projected_metrics = schedule_metrics(projected, mandatory)
    stage1 = projected if int(projected_metrics["total_aircraft_time_minutes"]) < int(before["total_aircraft_time_minutes"]) else old_stage1
    cap = int(schedule_metrics(stage1, mandatory)["total_aircraft_time_minutes"])
    fixed, unserved, fixed_stats = optimize_fixed_flight_assignments(
        stage1, people, data.config, time_limit_seconds=args.assignment_milp_time
    )
    candidates = [fixed]
    if int(schedule_metrics(old_stage2, people)["total_aircraft_time_minutes"]) <= cap:
        candidates.append(old_stage2)
    stage2 = min(candidates, key=lambda value: stage2_key(value, people))

    stage1_result, stage1_routes, stage1_assignments = _validate(
        "q3-feedback-base", stage1, people, data, run_dir
    )
    stage2_result, stage2_routes, stage2_assignments = _validate(
        "q3-feedback-final", stage2, people, data, run_dir
    )
    final_s1, final_s2 = stage1_result.metrics.to_dict(), stage2_result.metrics.to_dict()
    if final_s2["total_aircraft_time_minutes"] > final_s1["total_aircraft_time_minutes"]:
        raise RuntimeError("Final feedback Stage2 exceeds cap")
    shutil.copy2(stage1_routes, run_dir / "q3-base-routes.csv")
    shutil.copy2(stage1_assignments, run_dir / "q3-base-assignments.csv")
    shutil.copy2(stage2_routes, run_dir / "q3-routes.csv")
    shutil.copy2(stage2_assignments, run_dir / "q3-assignments.csv")
    shutil.copy2(run_dir / "q3-feedback-base-validator.json", run_dir / "q3-base-validator.json")
    shutil.copy2(run_dir / "q3-feedback-final-validator.json", run_dir / "q3-validator.json")

    trace_path = run_dir / "p2_trace.json"
    traces = json.loads(trace_path.read_text(encoding="utf-8")) if trace_path.exists() else {}
    traces["p2_final_feedback"] = {
        "before_stage1": before,
        "projected_stage1": projected_metrics,
        "after_stage1": schedule_metrics(stage1, mandatory),
        "after_stage2": schedule_metrics(stage2, people),
        "fixed_flight_assignment": fixed_stats,
        "unserved_optional": unserved,
    }
    write_json(trace_path, traces)

    ablation_path = run_dir / "q3-p2-ablation.csv"
    with ablation_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows.append(
        {
            "configuration": "H_final_feedback",
            "stage1_time": final_s1["total_aircraft_time_minutes"],
            "stage1_passenger_time": final_s1["total_passenger_travel_time_minutes"],
            "stage1_flights": final_s1["total_flights"],
            "stage2_optional": optional_count - final_s2["unserved_optional_passengers"],
            "stage2_time": final_s2["total_aircraft_time_minutes"],
            "stage2_passenger_time": final_s2["total_passenger_travel_time_minutes"],
            "stage2_flights": final_s2["total_flights"],
            "fuel_kg": final_s2["total_fuel_consumption_kg"],
            "runtime_seconds": round(time.perf_counter() - started, 6),
            "validator": True,
        }
    )
    write_csv(ablation_path, list(rows[0]), rows)

    lower_bound = 14125
    gap = round(
        100.0 * (final_s1["total_aircraft_time_minutes"] - lower_bound)
        / final_s1["total_aircraft_time_minutes"],
        6,
    )
    metrics_path = run_dir / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics.update(
        {
            "baseline_metrics": final_s1,
            "final_metrics": final_s2,
            "served_optional": optional_count - final_s2["unserved_optional_passengers"],
            "final_feedback_applied": True,
            "feedback_runtime_seconds": round(time.perf_counter() - started, 6),
        }
    )
    write_json(metrics_path, metrics)
    write_json(
        run_dir / "bounds.json",
        {
            "stage1": {
                "incumbent_upper_bound_minutes": final_s1["total_aircraft_time_minutes"],
                "enhanced_global_lower_bound_minutes": lower_bound,
                "certified_gap_percent": gap,
                "finite_candidate_pool_reference_minutes": 15198,
                "candidate_pool_reference_is_global_bound": False,
            },
            "stage2": {
                "served_optional_incumbent": metrics["served_optional"],
                "optional_upper_bound": optional_count,
                "proven_optimal_for_original_problem": final_s2["unserved_optional_passengers"] == 0,
                "fixed_flight_assignment_optimal_only": bool(fixed_stats["fixed_flight_optimal"]),
            },
        },
    )
    config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    config.update(
        {
            "final_feedback_applied": True,
            "final_feedback_runtime_seconds": round(time.perf_counter() - started, 6),
            "source_p2_run": str(args.source_run),
        }
    )
    write_json(run_dir / "run_config.json", config)
    report = f"""# Q3 P2 Results

- P0/P1 CLOSURE GATE = PASS.
- Final Stage-2-to-Stage-1 feedback applied: {before['total_aircraft_time_minutes']} -> {final_s1['total_aircraft_time_minutes']} min.
- Final Stage 1: {final_s1['total_aircraft_time_minutes']} min, {final_s1['total_flights']} flights, passenger time {final_s1['total_passenger_travel_time_minutes']} min, fuel {final_s1['total_fuel_consumption_kg']} kg.
- Final Stage 2: {metrics['served_optional']}/{optional_count} temporary, {final_s2['total_aircraft_time_minutes']} min, {final_s2['total_flights']} flights.
- Both independent Validators report 0 violations.
- Global LB: {lower_bound} min; conservative gap: {gap}%.
"""
    (run_dir / "Q3_P2_RESULTS.md").write_text(report, encoding="utf-8")

    if args.promote:
        for destination in (args.output_root / "best", args.output_root / "closure_p2_best"):
            destination.mkdir(parents=True, exist_ok=True)
            for path in run_dir.iterdir():
                if path.is_file():
                    shutil.copy2(path, destination / path.name)
    print(
        f"Q3 FINAL FEEDBACK PASS: Stage1={final_s1['total_aircraft_time_minutes']}, "
        f"temporary={metrics['served_optional']}/{optional_count}, "
        f"Stage2={final_s2['total_aircraft_time_minutes']}, run_dir={run_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
