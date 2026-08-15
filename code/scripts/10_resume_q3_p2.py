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
    export_q3_schedule,
    load_q3_people,
    load_q3_schedule,
    load_q3_variants,
    optimize_fixed_flight_assignments,
    project_mandatory_only,
    schedule_metrics,
)
from src.solver.q3_closure_p2 import (
    adaptive_structural_lns,
    augment_dynamic_route_pool,
    cross_day_flexible_descent,
    optional_feasibility_dossiers,
    stage1_key,
    stage2_key,
    targeted_optional_recovery,
)
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
    exported = result.metrics.to_dict()
    for key in (
        "total_aircraft_time_minutes",
        "total_passenger_travel_time_minutes",
        "total_flights",
        "total_fuel_consumption_kg",
    ):
        if abs(float(memory[key]) - float(exported[key])) > 1e-6:
            raise RuntimeError(f"{name} metric mismatch: {key}")
    return result, routes, assignments


def _row(name, stage1, stage2, mandatory, people, runtime=0.0):
    left = schedule_metrics(stage1, mandatory)
    right = schedule_metrics(stage2, people)
    return {
        "configuration": name,
        "stage1_time": left["total_aircraft_time_minutes"],
        "stage1_passenger_time": left["total_passenger_travel_time_minutes"],
        "stage1_flights": left["total_flights"],
        "stage2_optional": right["served_optional"],
        "stage2_time": right["total_aircraft_time_minutes"],
        "stage2_passenger_time": right["total_passenger_travel_time_minutes"],
        "stage2_flights": right["total_flights"],
        "fuel_kg": right["total_fuel_consumption_kg"],
        "runtime_seconds": round(runtime, 6),
        "validator": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume bounded P2 from a passed closure checkpoint")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--closure-run",
        type=Path,
        default=ROOT / "outputs/q3/runs/q3-closure-p2-v9-final",
    )
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs/q3")
    parser.add_argument("--variant-cache", type=Path, default=ROOT / "outputs/q2/pair_n3_h10.pkl")
    parser.add_argument("--assignment-milp-time", type=float, default=45.0)
    parser.add_argument("--p2a-trials", type=int, default=9)
    parser.add_argument("--p2b-trials-per-operator", type=int, default=1)
    parser.add_argument("--p2-master-seeds", type=int, default=3)
    parser.add_argument("--p2d-trials", type=int, default=5)
    parser.add_argument("--dynamic-sequences", type=int, default=6)
    parser.add_argument("--master-seed", type=int, default=20260815)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    gate = json.loads((args.closure_run / "closure_gate.json").read_text(encoding="utf-8"))
    if not gate.get("pass"):
        raise RuntimeError("Closure checkpoint did not pass; P2 cannot start")
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q3-p2"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(run_dir)
    run_dir.mkdir(parents=True)
    started = time.perf_counter()

    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    mandatory = {pid: person for pid, person in people.items() if person.mandatory}
    optional_count = len(people) - len(mandatory)
    variants = load_q3_variants(args.variant_cache, people.values(), data.config)
    stage1 = load_q3_schedule(
        args.closure_run / "closure-stage1-routes.csv",
        args.closure_run / "closure-stage1-assignments.csv",
        people,
        variants,
        data.config,
    )
    stage2 = load_q3_schedule(
        args.closure_run / "closure-stage2-routes.csv",
        args.closure_run / "closure-stage2-assignments.csv",
        people,
        variants,
        data.config,
    )
    closure_s1, closure_s2 = _validate("closure-input-stage1", stage1, people, data, run_dir)[0], _validate("closure-input-stage2", stage2, people, data, run_dir)[0]
    ablation = [_row("A_closure", stage1, stage2, mandatory, people)]
    traces = {}

    phase = time.perf_counter()
    dossiers = optional_feasibility_dossiers(stage2, people, variants, data)
    write_json(run_dir / "q3-p2a-feasibility-dossiers.json", dossiers)
    stage2, p2a = targeted_optional_recovery(
        stage2,
        people,
        variants,
        data,
        stage1_cap=int(schedule_metrics(stage1, mandatory)["total_aircraft_time_minutes"]),
        maximum_trials=args.p2a_trials,
        assignment_time_limit_seconds=args.assignment_milp_time,
    )
    traces["p2a"] = p2a
    ablation.append(_row("B_P2A", stage1, stage2, mandatory, people, time.perf_counter() - phase))

    phase = time.perf_counter()
    p2b_runs = []
    for seed_index in range(args.p2_master_seeds):
        candidate, trace = adaptive_structural_lns(
            stage1,
            mandatory,
            variants,
            data,
            stage=1,
            stage1_cap=None,
            minimum_optional_served=0,
            trials_per_operator=args.p2b_trials_per_operator,
            seed=args.master_seed + seed_index,
        )
        p2b_runs.append((candidate, trace))
    p2b_stage1, selected_trace = min(
        p2b_runs, key=lambda item: stage1_key(item[0], mandatory)
    )
    if stage1_key(p2b_stage1, mandatory) < stage1_key(stage1, mandatory):
        stage1 = p2b_stage1
    traces["p2b"] = {
        "selected": selected_trace,
        "seeds": [trace for _candidate, trace in p2b_runs],
    }
    projected = project_mandatory_only(stage2, people)
    if stage1_key(projected, mandatory) < stage1_key(stage1, mandatory):
        stage1 = projected
    cap = int(schedule_metrics(stage1, mandatory)["total_aircraft_time_minutes"])
    fixed, _unserved, fixed_stats = optimize_fixed_flight_assignments(
        stage1, people, data.config, time_limit_seconds=args.assignment_milp_time
    )
    candidates = [fixed]
    if int(schedule_metrics(stage2, people)["total_aircraft_time_minutes"]) <= cap:
        candidates.append(stage2)
    stage2 = min(candidates, key=lambda value: stage2_key(value, people))
    traces["p2b_fixed_stage2"] = fixed_stats
    write_json(run_dir / "q3-p2b-lns-trace.json", traces["p2b"])
    operator_rows = [
        {**row, "histogram": json.dumps(row["histogram"], ensure_ascii=False)}
        for trace in traces["p2b"]["seeds"]
        for row in trace["operator_stats"]
    ]
    write_csv(
        run_dir / "q3-p2b-operator-stats.csv",
        list(operator_rows[0]) if operator_rows else ["operator"],
        operator_rows,
    )
    ablation.append(_row("C_P2B", stage1, stage2, mandatory, people, time.perf_counter() - phase))

    phase = time.perf_counter()
    assigned = {pid for flight in stage2 for pid in flight.person_ids}
    targets = sorted(
        pid for pid, person in people.items() if not person.mandatory and pid not in assigned
    )
    augmented, p2c = augment_dynamic_route_pool(
        variants,
        targets,
        people,
        stage2,
        data,
        maximum_sequences=args.dynamic_sequences,
    )
    p2c_candidate, p2c_recovery = targeted_optional_recovery(
        stage2,
        people,
        augmented,
        data,
        stage1_cap=int(schedule_metrics(stage1, mandatory)["total_aircraft_time_minutes"]),
        maximum_trials=max(3, args.p2a_trials // 2),
        assignment_time_limit_seconds=args.assignment_milp_time,
    )
    if stage2_key(p2c_candidate, people) < stage2_key(stage2, people):
        stage2 = p2c_candidate
    traces["p2c"] = {"pool": p2c, "recovery": p2c_recovery}
    ablation.append(_row("D_P2C_dynamic", stage1, stage2, mandatory, people, time.perf_counter() - phase))

    phase = time.perf_counter()
    p2d_stage1, p2d1 = cross_day_flexible_descent(
        stage1,
        mandatory,
        augmented,
        data,
        stage=1,
        stage1_cap=None,
        minimum_optional_served=0,
        maximum_trials=args.p2d_trials,
    )
    if stage1_key(p2d_stage1, mandatory) < stage1_key(stage1, mandatory):
        stage1 = p2d_stage1
    cap = int(schedule_metrics(stage1, mandatory)["total_aircraft_time_minutes"])
    fixed, _unserved, fixed_stats = optimize_fixed_flight_assignments(
        stage1, people, data.config, time_limit_seconds=args.assignment_milp_time
    )
    if int(schedule_metrics(stage2, people)["total_aircraft_time_minutes"]) > cap or stage2_key(fixed, people) < stage2_key(stage2, people):
        stage2 = fixed
    p2d_stage2, p2d2 = cross_day_flexible_descent(
        stage2,
        people,
        augmented,
        data,
        stage=2,
        stage1_cap=cap,
        minimum_optional_served=int(schedule_metrics(stage2, people)["served_optional"]),
        maximum_trials=args.p2d_trials,
    )
    if stage2_key(p2d_stage2, people) < stage2_key(stage2, people):
        stage2 = p2d_stage2
    traces["p2d"] = {"stage1": p2d1, "fixed_stage2": fixed_stats, "stage2": p2d2}
    write_json(run_dir / "q3-p2d-crossday-trace.json", traces["p2d"])
    ablation.append(_row("G_full_integrated", stage1, stage2, mandatory, people, time.perf_counter() - phase))

    # Mandatory final feedback: any shorter Stage-2 structure is a valid
    # Stage-1 candidate after optional assignments are deleted.
    feedback_before = schedule_metrics(stage1, mandatory)
    feedback_projection = project_mandatory_only(stage2, people)
    projection_metrics = schedule_metrics(feedback_projection, mandatory)
    if int(projection_metrics["total_aircraft_time_minutes"]) < int(
        feedback_before["total_aircraft_time_minutes"]
    ):
        stage1 = feedback_projection
    cap = int(schedule_metrics(stage1, mandatory)["total_aircraft_time_minutes"])
    feedback_fixed, _feedback_unserved, feedback_stats = optimize_fixed_flight_assignments(
        stage1, people, data.config, time_limit_seconds=args.assignment_milp_time
    )
    feedback_candidates = [feedback_fixed]
    if int(schedule_metrics(stage2, people)["total_aircraft_time_minutes"]) <= cap:
        feedback_candidates.append(stage2)
    stage2 = min(feedback_candidates, key=lambda value: stage2_key(value, people))
    traces["p2_final_feedback"] = {
        "before_stage1": feedback_before,
        "projected_stage1": projection_metrics,
        "after_stage1": schedule_metrics(stage1, mandatory),
        "after_stage2": schedule_metrics(stage2, people),
        "fixed_flight_assignment": feedback_stats,
    }
    ablation.append(_row("H_final_feedback", stage1, stage2, mandatory, people))

    final_s1_result, s1_routes, s1_assignments = _validate(
        "q3-closure-p2-base", stage1, people, data, run_dir
    )
    final_s2_result, s2_routes, s2_assignments = _validate(
        "q3-closure-p2-final", stage2, people, data, run_dir
    )
    final_s1, final_s2 = final_s1_result.metrics.to_dict(), final_s2_result.metrics.to_dict()
    if final_s1["total_aircraft_time_minutes"] > closure_s1.metrics.total_aircraft_time_minutes:
        raise RuntimeError("P2 Stage 1 regressed against closure")
    if final_s2["total_aircraft_time_minutes"] > final_s1["total_aircraft_time_minutes"]:
        raise RuntimeError("P2 Stage 2 exceeds cap")
    shutil.copy2(s1_routes, run_dir / "q3-base-routes.csv")
    shutil.copy2(s1_assignments, run_dir / "q3-base-assignments.csv")
    shutil.copy2(s2_routes, run_dir / "q3-routes.csv")
    shutil.copy2(s2_assignments, run_dir / "q3-assignments.csv")
    shutil.copy2(run_dir / "q3-closure-p2-base-validator.json", run_dir / "q3-base-validator.json")
    shutil.copy2(run_dir / "q3-closure-p2-final-validator.json", run_dir / "q3-validator.json")
    write_json(run_dir / "p2_trace.json", traces)
    write_csv(run_dir / "q3-p2-ablation.csv", list(ablation[0]), ablation)

    lower_bound = 14125
    gap = round(
        100.0 * (final_s1["total_aircraft_time_minutes"] - lower_bound)
        / final_s1["total_aircraft_time_minutes"],
        6,
    )
    metrics = {
        "gate_pass": True,
        "closure_gate": gate,
        "baseline_metrics": final_s1,
        "final_metrics": final_s2,
        "served_optional": optional_count - final_s2["unserved_optional_passengers"],
        "runtime_seconds": round(time.perf_counter() - started, 6),
    }
    write_json(run_dir / "metrics.json", metrics)
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
            },
        },
    )
    write_json(
        run_dir / "run_config.json",
        {
            **{key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
            "run_id": run_id,
            "total_elapsed_seconds": round(time.perf_counter() - started, 6),
            "combination_budget_per_neighborhood": 12,
        },
    )
    report = f"""# Q3 P2 Results

- P0/P1 CLOSURE GATE = PASS.
- Final Stage 1: {final_s1['total_aircraft_time_minutes']} min, {final_s1['total_flights']} flights, passenger time {final_s1['total_passenger_travel_time_minutes']} min, fuel {final_s1['total_fuel_consumption_kg']} kg.
- Final Stage 2: {metrics['served_optional']}/{optional_count} temporary, {final_s2['total_aircraft_time_minutes']} min, {final_s2['total_flights']} flights.
- Stage 1 and Stage 2 independent Validators both report 0 violations.
- Global LB: {lower_bound} min; conservative certified gap: {gap}%.
- Candidate-pool reference 15198 min is not a global lower bound.
"""
    (run_dir / "Q3_P2_RESULTS.md").write_text(report, encoding="utf-8")
    (run_dir / "Q3_P3_HANDOFF.md").write_text(
        "# Q3 P3 Handoff\n\nP3 is not implemented. Next: restricted master, column generation, dual-guided pricing, resource cuts and stronger certificates.\n",
        encoding="utf-8",
    )
    for name in ("P0P1_CLOSURE_AUDIT.md", "Q3_P0P1_CLOSURE_REPORT.md", "closure_gate.json", "multistart.json", "multistart.csv", "route_cache_provenance.json", "repo_state.json", "feedback_trace.json"):
        source = args.closure_run / name
        if source.exists():
            shutil.copy2(source, run_dir / name)

    if args.promote:
        for destination in (args.output_root / "best", args.output_root / "closure_p2_best"):
            destination.mkdir(parents=True, exist_ok=True)
            for path in run_dir.iterdir():
                if path.is_file():
                    shutil.copy2(path, destination / path.name)
    print(
        f"Q3 P2 PASS: Stage1={final_s1['total_aircraft_time_minutes']}, "
        f"temporary={metrics['served_optional']}/{optional_count}, "
        f"Stage2={final_s2['total_aircraft_time_minutes']}, run_dir={run_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
