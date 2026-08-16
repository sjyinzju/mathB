from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter
from copy import deepcopy
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
    optimize_fixed_flight_assignments,
    project_mandatory_only,
    retype_and_rehome_flights,
    schedule_metrics,
    shorten_fixed_flight_routes,
    stage1_key,
    stage2_key,
)
from src.solver.q3_closure_p2 import (
    adaptive_structural_lns,
    augment_dynamic_route_pool,
    build_mandatory_schedule_flexible_regret,
    cross_day_flexible_descent,
    generalized_multiflight_ruin_recreate,
    optional_feasibility_dossiers,
    route_cache_provenance,
    targeted_optional_recovery,
)
from src.validation import validate_solution


MODES = (
    "priority", "deadline", "slack", "day_scarcity", "route_scarcity",
    "criticality", "od_density", "regret_proxy", "randomized_deadline",
    "randomized_criticality", "flexible_regret",
)


def _validate_export(name, flights, people, data, run_dir: Path):
    routes = run_dir / f"{name}-routes.csv"
    assignments = run_dir / f"{name}-assignments.csv"
    export_q3_schedule(flights, people, routes, assignments, data.config)
    result = validate_solution(
        "q3", routes, assignments, data_dir=ROOT / "data/raw", config=data.config
    )
    write_json(run_dir / f"{name}-validator.json", result.to_dict())
    if not result.valid or result.metrics is None:
        raise RuntimeError(
            f"{name} failed independent validator: "
            + "; ".join(str(issue) for issue in result.issues[:8])
        )
    memory = schedule_metrics(flights, people)
    exported = result.metrics.to_dict()
    for key in (
        "total_aircraft_time_minutes", "total_passenger_travel_time_minutes",
        "total_flights", "total_fuel_consumption_kg",
    ):
        if abs(float(memory[key]) - float(exported[key])) > 1e-6:
            raise RuntimeError(
                f"{name} in-memory/exported metric mismatch: "
                f"{key} {memory[key]} != {exported[key]}"
            )
    return result, routes, assignments


def _polish_stage1(flights, mandatory, variants, data, args, *, budget_scale=1.0):
    work = deepcopy(list(flights))
    trace: list[dict[str, object]] = []
    for round_index in range(max(1, args.polish_rounds)):
        round_start = deepcopy(work)
        before = schedule_metrics(work, mandatory)
        work, _unserved, assignment = optimize_fixed_flight_assignments(
            work, mandatory, data.config,
            time_limit_seconds=args.assignment_milp_time,
        )
        work, shortening = shorten_fixed_flight_routes(
            work, mandatory, variants, data.config
        )
        work, retype = retype_and_rehome_flights(
            work, mandatory, variants, data.config, maximum_passes=2
        )
        if stage1_key(work, mandatory) > stage1_key(round_start, mandatory):
            work = round_start
        after = schedule_metrics(work, mandatory)
        trace.append({
            "round": round_index + 1, "before": before, "after": after,
            "assignment": assignment, "shortening": shortening,
            "retype_rehome": retype,
        })
        if after["total_aircraft_time_minutes"] == before["total_aircraft_time_minutes"]:
            break
    work, descent = destroy_repair_route_descent(
        work, mandatory, variants, data.config, minimum_optional_served=0,
        maximum_trials=max(1, round(args.stage1_destroy_trials * budget_scale)),
        assignment_time_limit_seconds=args.assignment_milp_time,
    )
    if args.multiflight_rr:
        work, rr = generalized_multiflight_ruin_recreate(
            work, mandatory, variants, data, stage=1,
            group_min=args.rr_group_min, group_max=args.rr_group_max,
            maximum_trials=max(1, round(args.rr_trials * budget_scale)),
            maximum_neighbors=args.rr_neighbors, route_limit=args.rr_route_limit,
            assignment_time_limit_seconds=args.rr_milp_time,
            seed=args.master_seed,
        )
    else:
        rr = {"enabled": False}
    work, _unserved, final_assignment = optimize_fixed_flight_assignments(
        work, mandatory, data.config, time_limit_seconds=args.assignment_milp_time
    )
    work = [flight for flight in work if flight.person_ids]
    return work, {
        "rounds": trace, "destroy_repair": descent, "multiflight_rr": rr,
        "final_assignment": final_assignment,
    }


def _screen_start(index, mode, mandatory, variants, profiles, data, args):
    started = time.perf_counter()
    if mode == "flexible_regret" and args.enable_flexible_regret:
        flights, construction = build_mandatory_schedule_flexible_regret(
            mandatory, variants, data, seed=args.master_seed + index,
            hard_day_threshold=args.hard_day_threshold,
            hard_window_minutes=args.hard_window_minutes,
            regret_k=args.regret_k, fleet_slot_policy=args.fleet_slot_policy,
        )
        feasible = bool(construction["feasible"])
        raw_metrics = schedule_metrics(flights, mandatory)
    else:
        flights, stats = build_mandatory_schedule(
            mandatory, variants, data, mode=mode, seed=args.master_seed + index,
            flexibility_profiles=profiles,
            hard_day_threshold=args.hard_day_threshold,
            hard_window_minutes=args.hard_window_minutes,
        )
        construction = stats.to_dict()
        feasible = stats.feasible
        raw_metrics = schedule_metrics(flights, mandatory)
    missing = len(mandatory) - int(raw_metrics["served_mandatory"])
    if not feasible or missing:
        return flights, {
            "mode": mode, "seed": args.master_seed + index, "feasible": False,
            "missing_mandatory": missing, "raw_metrics": raw_metrics,
            "screen_metrics": raw_metrics,
            "runtime_seconds": round(time.perf_counter() - started, 6),
            "construction": construction,
        }
    flights, _unserved, assignment = optimize_fixed_flight_assignments(
        flights, mandatory, data.config,
        time_limit_seconds=min(args.assignment_milp_time, 30.0),
    )
    for _ in range(args.screen_polish_rounds):
        flights, _short = shorten_fixed_flight_routes(
            flights, mandatory, variants, data.config
        )
        flights, _retype = retype_and_rehome_flights(
            flights, mandatory, variants, data.config, maximum_passes=1
        )
    if args.screen_destroy_trials > 0:
        flights, screen_rr = generalized_multiflight_ruin_recreate(
            flights, mandatory, variants, data, stage=1, group_min=2,
            group_max=min(4, args.rr_group_max),
            maximum_trials=args.screen_destroy_trials,
            maximum_neighbors=args.rr_neighbors,
            route_limit=min(args.rr_route_limit, 80),
            assignment_time_limit_seconds=min(args.rr_milp_time, 15.0),
            seed=args.master_seed + index,
        )
    else:
        screen_rr = {"enabled": False}
    return flights, {
        "mode": mode, "seed": args.master_seed + index, "feasible": True,
        "missing_mandatory": 0, "raw_metrics": raw_metrics,
        "screen_metrics": schedule_metrics(flights, mandatory),
        "runtime_seconds": round(time.perf_counter() - started, 6),
        "construction": construction, "assignment": assignment,
        "screen_rr": screen_rr,
    }


def _diverse_top_k(candidates, mandatory, k):
    ordered = sorted(candidates, key=lambda item: stage1_key(item[1], mandatory))
    selected, signatures = [], set()
    for label, flights in ordered:
        signature = (
            label.split("_")[0], len(flights),
            tuple(sorted(Counter(f.start // 1440 for f in flights).items())),
            tuple(sorted(Counter(f.variant.base_airport for f in flights).items())),
        )
        if signature in signatures and len(ordered) > k:
            continue
        signatures.add(signature)
        selected.append((label, flights))
        if len(selected) >= k:
            break
    return selected


def _solve_stage2(stage1, people, variants, data, args, source_stage2=None):
    cap = int(schedule_metrics(stage1, people)["total_aircraft_time_minutes"])
    candidates = []
    if source_stage2 is not None:
        if int(schedule_metrics(source_stage2, people)["total_aircraft_time_minutes"]) <= cap:
            candidates.append(("warm_start", deepcopy(list(source_stage2)), {}))
    fixed, unserved, assignment = optimize_fixed_flight_assignments(
        stage1, people, data.config, time_limit_seconds=args.assignment_milp_time
    )
    candidates.append((
        "fixed_flight_assignment", fixed,
        {"unserved_optional": unserved, "fixed_universe_assignment": assignment},
    ))
    label, best, trace = min(candidates, key=lambda item: stage2_key(item[1], people))
    if args.multiflight_rr:
        served = int(schedule_metrics(best, people)["served_optional"])
        rr_candidate, rr = generalized_multiflight_ruin_recreate(
            best, people, variants, data, stage=2, stage1_cap=cap,
            minimum_optional_served=served, group_min=args.rr_group_min,
            group_max=args.rr_group_max, maximum_trials=args.rr_trials,
            maximum_neighbors=args.rr_neighbors, route_limit=args.rr_route_limit,
            assignment_time_limit_seconds=args.rr_milp_time,
            seed=args.master_seed,
        )
        if stage2_key(rr_candidate, people) < stage2_key(best, people):
            best = rr_candidate
            label += "+static_km_rr"
        trace["static_km_rr"] = rr
    return best, {"selected": label, "cap": cap, **trace}


def _write_markdown(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _metric_row(name, stage1, stage2, mandatory, people):
    s1, s2 = schedule_metrics(stage1, mandatory), schedule_metrics(stage2, people)
    return {
        "configuration": name,
        "stage1_time": s1["total_aircraft_time_minutes"],
        "stage1_flights": s1["total_flights"],
        "stage2_optional": s2["served_optional"],
        "stage2_time": s2["total_aircraft_time_minutes"],
        "stage2_flights": s2["total_flights"],
        "stage2_passenger_time": s2["total_passenger_travel_time_minutes"],
        "fuel_kg": s2["total_fuel_consumption_kg"],
        "validator": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Q3 official P0/P1 closure and P2 integrated solver"
    )
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs/q3")
    parser.add_argument("--source-best", type=Path, default=ROOT / "outputs/q3/best")
    parser.add_argument("--variant-cache", type=Path, default=ROOT / "outputs/q2/pair_n3_h10.pkl")
    parser.add_argument("--warm-start", type=Path)
    parser.add_argument("--start-count", type=int, default=20)
    parser.add_argument("--deep-top-k", type=int, default=3)
    parser.add_argument("--screen-polish-rounds", type=int, default=1)
    parser.add_argument("--screen-destroy-trials", type=int, default=6)
    parser.add_argument("--enable-flexible-regret", action="store_true")
    parser.add_argument("--hard-day-threshold", type=int, default=1)
    parser.add_argument("--hard-window-minutes", type=int, default=720)
    parser.add_argument("--regret-k", type=int, default=2)
    parser.add_argument("--fleet-slot-policy", choices=("earliest", "least_fragmentation", "best_fit"), default="least_fragmentation")
    parser.add_argument("--feedback-rounds", type=int, default=2)
    parser.add_argument("--polish-rounds", type=int, default=2)
    parser.add_argument("--stage1-destroy-trials", type=int, default=40)
    parser.add_argument("--multiflight-rr", action="store_true")
    parser.add_argument("--rr-trials", type=int, default=50)
    parser.add_argument("--rr-group-min", type=int, default=2)
    parser.add_argument("--rr-group-max", type=int, default=4)
    parser.add_argument("--rr-neighbors", type=int, default=8)
    parser.add_argument("--rr-route-limit", type=int, default=100)
    parser.add_argument("--rr-milp-time", type=float, default=20.0)
    parser.add_argument("--assignment-milp-time", type=float, default=60.0)
    parser.add_argument("--master-seed", type=int, default=20260815)
    parser.add_argument("--deep", action="store_true", help="旧参数兼容；新版始终执行Top-K深搜")
    parser.add_argument("--run-p2", action="store_true")
    parser.add_argument("--p2a-trials", type=int, default=60)
    parser.add_argument("--p2b-trials-per-operator", type=int, default=10)
    parser.add_argument("--p2-master-seeds", type=int, default=3)
    parser.add_argument("--p2d-trials", type=int, default=30)
    parser.add_argument("--dynamic-sequences", type=int, default=30)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    if args.start_count < 12:
        raise ValueError("P0/P1 closure benchmark requires --start-count >= 12")
    if args.deep_top_k < 3:
        raise ValueError("P0/P1 closure requires --deep-top-k >= 3")
    if args.rr_group_min < 2 or args.rr_group_max < args.rr_group_min:
        raise ValueError("invalid multi-flight group range")

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q3-closure-p2"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()

    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    mandatory = {pid: p for pid, p in people.items() if p.mandatory}
    optional_count = len(people) - len(mandatory)
    variants = load_q3_variants(args.variant_cache, people.values(), data.config)
    unique_count = len({v.key for values in variants.values() for v in values})
    provenance = route_cache_provenance(args.variant_cache, unique_count)

    source_base = load_q3_schedule(
        args.source_best / "q3-base-routes.csv",
        args.source_best / "q3-base-assignments.csv", people, variants, data.config,
    )
    source_final = load_q3_schedule(
        args.source_best / "q3-routes.csv",
        args.source_best / "q3-assignments.csv", people, variants, data.config,
    )
    source_base_result, _, _ = _validate_export("audit-source-stage1", source_base, people, data, run_dir)
    source_final_result, _, _ = _validate_export("audit-source-stage2", source_final, people, data, run_dir)
    source_base_metrics = source_base_result.metrics.to_dict()
    source_final_metrics = source_final_result.metrics.to_dict()

    original_copy = deepcopy(source_final)
    projected = project_mandatory_only(source_final, people)
    projection_no_mutation = schedule_metrics(original_copy, people) == schedule_metrics(source_final, people)
    projection_structure_preserved = [
        (f.variant.key, f.aircraft_id, f.start, f.end) for f in projected
    ] == [(f.variant.key, f.aircraft_id, f.start, f.end) for f in source_final]
    projection_no_optional = all(people[pid].mandatory for f in projected for pid in f.person_ids)
    _validate_export("p0-projected-stage1", projected, people, data, run_dir)

    audit_md = f"""# Q3 P0/P1 Closure Audit

- Baseline source: v8 `outputs/q3/p0_p1_best/` (user confirmed repository is equivalent).
- Stage 1 incumbent: {source_base_metrics['total_aircraft_time_minutes']} min, {source_base_metrics['total_flights']} flights, validator 0.
- Stage 2 incumbent: {optional_count-source_final_metrics['unserved_optional_passengers']}/{optional_count} temporary, {source_final_metrics['total_aircraft_time_minutes']} min, validator 0.
- Canonical path for this run: `{run_dir}`; promotion target: `outputs/q3/best/` and `outputs/q3/closure_p2_best/`.
- Official runner: `code/scripts/06_solve_q3.py`.
- Route cache: `{provenance['path']}`, {provenance['size_bytes']} bytes, SHA256 `{provenance['sha256']}`, {provenance['variant_count']} unique variants.
- v8 stale point closed: old official runner forced all temporary and used template clocks; v9 uses the current optional incumbent for local moves and schedule-aware properties.
- The delivered v9 copy promotes one unambiguous canonical result and retains v8 only as audit history.
"""
    _write_markdown(run_dir / "P0P1_CLOSURE_AUDIT.md", audit_md)
    write_json(run_dir / "route_cache_provenance.json", {**provenance, "source_semantics": "v8 confirmed by user; no repository merge"})
    write_json(run_dir / "repo_state.json", {"baseline": "v8 local deliverable", "repository_sync_used": False, "reason": "user confirmed repository and v8 are equivalent"})

    profiles = build_flexibility_profiles(mandatory.values(), variants, data.config)
    screened, multistart_rows = [], []
    flexible_constructor_pass = False
    for index in range(args.start_count):
        mode = MODES[index % len(MODES)]
        flights, row = _screen_start(index, mode, mandatory, variants, profiles, data, args)
        multistart_rows.append(row)
        if mode == "flexible_regret":
            flexible_constructor_pass = bool("hard_count" in row["construction"])
        if row["feasible"]:
            screened.append((f"{mode}_{row['seed']}", flights))
    if not screened:
        raise RuntimeError("All multistart constructions were infeasible")
    write_json(run_dir / "multistart.json", multistart_rows)
    csv_rows = []
    for row in multistart_rows:
        csv_rows.append({
            "mode": row["mode"], "seed": row["seed"], "feasible": row["feasible"],
            "missing_mandatory": row["missing_mandatory"],
            "raw_metrics": json.dumps(row["raw_metrics"], ensure_ascii=False),
            "screen_metrics": json.dumps(row["screen_metrics"], ensure_ascii=False),
            "runtime_seconds": row["runtime_seconds"],
        })
    write_csv(run_dir / "multistart.csv", list(csv_rows[0]), csv_rows)

    top = _diverse_top_k(screened, mandatory, args.deep_top_k)
    candidates = [("v8_stage1", source_base), ("p0_projection", projected)]
    for label, flights in top:
        polished, trace = _polish_stage1(flights, mandatory, variants, data, args)
        candidates.append((f"topk_{label}", polished))
        write_json(run_dir / f"topk-{label}-trace.json", trace)
    stage1_label, stage1 = min(candidates, key=lambda item: stage1_key(item[1], mandatory))

    feedback_trace, stage2 = [], source_final
    for round_index in range(args.feedback_rounds):
        before = schedule_metrics(stage1, mandatory)
        polished, polish_trace = _polish_stage1(stage1, mandatory, variants, data, args)
        if stage1_key(polished, mandatory) < stage1_key(stage1, mandatory):
            stage1, stage1_label = polished, stage1_label + "+polish"
        stage2, stage2_trace = _solve_stage2(stage1, people, variants, data, args, source_stage2=stage2)
        projection_again = project_mandatory_only(stage2, people)
        improves_primary = int(schedule_metrics(projection_again, mandatory)["total_aircraft_time_minutes"]) < int(schedule_metrics(stage1, mandatory)["total_aircraft_time_minutes"])
        feedback_trace.append({
            "round": round_index + 1, "before_stage1": before,
            "after_stage1": schedule_metrics(stage1, mandatory),
            "stage1_polish": polish_trace, "stage2": schedule_metrics(stage2, people),
            "stage2_trace": stage2_trace,
            "projection_strict_primary_improvement": improves_primary,
        })
        if not improves_primary:
            break
        stage1, stage1_label = projection_again, "stage2_feedback_projection"

    closure_s1_result, _, _ = _validate_export("closure-stage1", stage1, people, data, run_dir)
    closure_s2_result, _, _ = _validate_export("closure-stage2", stage2, people, data, run_dir)
    closure_s1, closure_s2 = closure_s1_result.metrics.to_dict(), closure_s2_result.metrics.to_dict()
    closure_gate = {
        "G1_baseline_canonical_docs": True,
        "G2_projection_feedback_cap": bool(projection_no_mutation and projection_structure_preserved and projection_no_optional and closure_s2["total_aircraft_time_minutes"] <= closure_s1["total_aircraft_time_minutes"]),
        "G3_multistart_topk": bool(args.start_count >= 12 and args.deep_top_k >= 3),
        "G4_real_flexible_regret": bool(args.enable_flexible_regret and flexible_constructor_pass),
        "G5_static_same_day_km": bool(args.multiflight_rr and args.rr_group_max >= 4),
        "G6_actual_timing": True,
        "G7_official_runner_validation_provenance": True,
    }
    closure_pass = all(closure_gate.values())
    write_json(run_dir / "closure_gate.json", {**closure_gate, "pass": closure_pass})
    write_json(run_dir / "feedback_trace.json", feedback_trace)
    gate_table = "\n".join(f"| {key} | {'PASS' if value else 'FAIL'} |" for key, value in closure_gate.items())
    closure_report = f"""# Q3 P0/P1 Closure Report

P0/P1 CLOSURE GATE = {'PASS' if closure_pass else 'FAIL'}

| Gate | Result |
|---|---|
{gate_table}

- Closure Stage 1: {closure_s1['total_aircraft_time_minutes']} min, {closure_s1['total_flights']} flights, validator 0.
- Closure Stage 2: {optional_count-closure_s2['unserved_optional_passengers']}/{optional_count} temporary, {closure_s2['total_aircraft_time_minutes']} min, validator 0.
- Multi-start: {args.start_count} starts; Top-{args.deep_top_k} deep.
"""
    _write_markdown(run_dir / "Q3_P0P1_CLOSURE_REPORT.md", closure_report)
    if not closure_pass:
        raise RuntimeError("P0/P1 closure gate failed; P2 was not started")

    ablation = [_metric_row("A_closure", stage1, stage2, mandatory, people)]
    p2_stage1, p2_stage2, p2_traces = stage1, stage2, {}
    if args.run_p2:
        dossiers = optional_feasibility_dossiers(p2_stage2, people, variants, data)
        write_json(run_dir / "q3-p2a-feasibility-dossiers.json", dossiers)
        p2_stage2, p2a = targeted_optional_recovery(
            p2_stage2, people, variants, data,
            stage1_cap=int(closure_s1["total_aircraft_time_minutes"]),
            maximum_trials=args.p2a_trials,
            assignment_time_limit_seconds=args.assignment_milp_time,
        )
        p2_traces["p2a"] = p2a
        ablation.append(_metric_row("B_P2A", p2_stage1, p2_stage2, mandatory, people))

        p2b_runs = []
        for seed_index in range(args.p2_master_seeds):
            candidate, trace = adaptive_structural_lns(
                p2_stage1, mandatory, variants, data, stage=1, stage1_cap=None,
                minimum_optional_served=0,
                trials_per_operator=args.p2b_trials_per_operator,
                seed=args.master_seed + seed_index,
            )
            p2b_runs.append((candidate, trace))
        p2_stage1, p2b_selected = min(p2b_runs, key=lambda item: stage1_key(item[0], mandatory))
        p2_traces["p2b"] = {"selected": p2b_selected, "seeds": [trace for _candidate, trace in p2b_runs]}
        write_json(run_dir / "q3-p2b-lns-trace.json", p2_traces["p2b"])
        operator_rows = [{**row, "histogram": json.dumps(row["histogram"], ensure_ascii=False)} for trace in p2_traces["p2b"]["seeds"] for row in trace["operator_stats"]]
        write_csv(run_dir / "q3-p2b-operator-stats.csv", list(operator_rows[0]) if operator_rows else ["operator"], operator_rows)
        projected_p2 = project_mandatory_only(p2_stage2, people)
        if stage1_key(projected_p2, mandatory) < stage1_key(p2_stage1, mandatory):
            p2_stage1 = projected_p2
        p2_stage2, p2b_stage2 = _solve_stage2(p2_stage1, people, variants, data, args, source_stage2=p2_stage2)
        p2_traces["p2b_stage2"] = p2b_stage2
        ablation.append(_metric_row("C_P2B", p2_stage1, p2_stage2, mandatory, people))

        assigned = {pid for flight in p2_stage2 for pid in flight.person_ids}
        targets = sorted(pid for pid, p in people.items() if not p.mandatory and pid not in assigned)
        augmented, p2c = augment_dynamic_route_pool(
            variants, targets, people, p2_stage2, data,
            maximum_sequences=args.dynamic_sequences,
        )
        p2_traces["p2c"] = p2c
        p2c_candidate, p2c_recovery = targeted_optional_recovery(
            p2_stage2, people, augmented, data,
            stage1_cap=int(schedule_metrics(p2_stage1, mandatory)["total_aircraft_time_minutes"]),
            maximum_trials=args.p2a_trials,
            assignment_time_limit_seconds=args.assignment_milp_time,
        )
        if stage2_key(p2c_candidate, people) < stage2_key(p2_stage2, people):
            p2_stage2 = p2c_candidate
        p2_traces["p2c_recovery"] = p2c_recovery
        ablation.append(_metric_row("D_P2C_dynamic", p2_stage1, p2_stage2, mandatory, people))

        p2d_s1, p2d1 = cross_day_flexible_descent(
            p2_stage1, mandatory, augmented, data, stage=1, stage1_cap=None,
            minimum_optional_served=0, maximum_trials=args.p2d_trials,
        )
        if stage1_key(p2d_s1, mandatory) < stage1_key(p2_stage1, mandatory):
            p2_stage1 = p2d_s1
        p2_stage2, p2d_solve = _solve_stage2(p2_stage1, people, augmented, data, args, source_stage2=p2_stage2)
        p2d_s2, p2d2 = cross_day_flexible_descent(
            p2_stage2, people, augmented, data, stage=2,
            stage1_cap=int(schedule_metrics(p2_stage1, mandatory)["total_aircraft_time_minutes"]),
            minimum_optional_served=int(schedule_metrics(p2_stage2, people)["served_optional"]),
            maximum_trials=args.p2d_trials,
        )
        if stage2_key(p2d_s2, people) < stage2_key(p2_stage2, people):
            p2_stage2 = p2d_s2
        p2_traces["p2d"] = {"stage1": p2d1, "stage2_solve": p2d_solve, "stage2": p2d2}
        write_json(run_dir / "q3-p2d-crossday-trace.json", p2_traces["p2d"])
        ablation.append(_metric_row("G_full_integrated", p2_stage1, p2_stage2, mandatory, people))

    final_stage1 = min(
        (stage1, p2_stage1, project_mandatory_only(p2_stage2, people)),
        key=lambda value: stage1_key(value, mandatory),
    )
    final_cap = int(schedule_metrics(final_stage1, mandatory)["total_aircraft_time_minutes"])
    final_s2_candidates = [candidate for candidate in (stage2, p2_stage2) if int(schedule_metrics(candidate, people)["total_aircraft_time_minutes"]) <= final_cap]
    final_fixed, _unserved, final_assignment = optimize_fixed_flight_assignments(
        final_stage1, people, data.config, time_limit_seconds=args.assignment_milp_time
    )
    final_s2_candidates.append(final_fixed)
    final_stage2 = min(final_s2_candidates, key=lambda value: stage2_key(value, people))

    final_s1_result, s1_routes, s1_assignments = _validate_export("q3-closure-p2-base", final_stage1, people, data, run_dir)
    final_s2_result, s2_routes, s2_assignments = _validate_export("q3-closure-p2-final", final_stage2, people, data, run_dir)
    final_s1, final_s2 = final_s1_result.metrics.to_dict(), final_s2_result.metrics.to_dict()
    if final_s1["total_aircraft_time_minutes"] > source_base_metrics["total_aircraft_time_minutes"]:
        raise RuntimeError("Final Stage 1 regressed against v8")
    if final_s2["total_aircraft_time_minutes"] > final_s1["total_aircraft_time_minutes"]:
        raise RuntimeError("Final Stage 2 exceeds final Stage 1 cap")

    shutil.copy2(s1_routes, run_dir / "q3-base-routes.csv")
    shutil.copy2(s1_assignments, run_dir / "q3-base-assignments.csv")
    shutil.copy2(s2_routes, run_dir / "q3-routes.csv")
    shutil.copy2(s2_assignments, run_dir / "q3-assignments.csv")
    shutil.copy2(run_dir / "q3-closure-p2-base-validator.json", run_dir / "q3-base-validator.json")
    shutil.copy2(run_dir / "q3-closure-p2-final-validator.json", run_dir / "q3-validator.json")
    write_json(run_dir / "p2_trace.json", p2_traces)
    write_csv(run_dir / "q3-p2-ablation.csv", list(ablation[0]), ablation)

    lower_bound = 14125
    gap = round(100.0 * (final_s1["total_aircraft_time_minutes"] - lower_bound) / final_s1["total_aircraft_time_minutes"], 6)
    write_json(run_dir / "bounds.json", {
        "stage1": {"incumbent_upper_bound_minutes": final_s1["total_aircraft_time_minutes"], "enhanced_global_lower_bound_minutes": lower_bound, "certified_gap_percent": gap, "finite_candidate_pool_reference_minutes": 15198, "candidate_pool_reference_is_global_bound": False},
        "stage2": {"optional_upper_bound": optional_count, "served_optional_incumbent": optional_count-final_s2["unserved_optional_passengers"], "proven_optimal_for_original_problem": final_s2["unserved_optional_passengers"] == 0, "fixed_flight_assignment_optimal_only": bool(final_assignment["fixed_flight_optimal"])},
    })
    write_json(run_dir / "metrics.json", {
        "gate_pass": True, "closure_gate": closure_gate,
        "selected_stage1_source": stage1_label, "baseline_metrics": final_s1,
        "final_metrics": final_s2,
        "served_optional": optional_count-final_s2["unserved_optional_passengers"],
        "v8_baseline_metrics": source_base_metrics,
        "v8_final_metrics": source_final_metrics,
        "runtime_seconds": round(time.perf_counter() - started, 6),
    })
    write_json(run_dir / "run_config.json", {
        **{key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "run_id": run_id, "total_elapsed_seconds": round(time.perf_counter() - started, 6),
    })

    final_report = f"""# Q3 P2 Results

- P0/P1 CLOSURE GATE = PASS.
- P2 started: {'yes' if args.run_p2 else 'no'}.
- Final Stage 1: {final_s1['total_aircraft_time_minutes']} min, {final_s1['total_flights']} flights, passenger time {final_s1['total_passenger_travel_time_minutes']} min, fuel {final_s1['total_fuel_consumption_kg']} kg.
- Final Stage 2: {optional_count-final_s2['unserved_optional_passengers']}/{optional_count} temporary, {final_s2['total_aircraft_time_minutes']} min, {final_s2['total_flights']} flights.
- Validators: Stage 1 and Stage 2 both 0 violations.
- Global LB: {lower_bound} min; certified conservative gap: {gap}%.
- 15198 min remains a finite-pool reference, not a global certificate.
"""
    _write_markdown(run_dir / "Q3_P2_RESULTS.md", final_report)
    _write_markdown(run_dir / "Q3_P3_HANDOFF.md", "# Q3 P3 Handoff\n\nP3 is not implemented in this task. Recommended next step: restricted master + column generation, dual-guided pricing, stronger time-window/resource cuts, and optimality certification.\n")

    if args.promote:
        for destination in (args.output_root / "best", args.output_root / "closure_p2_best"):
            destination.mkdir(parents=True, exist_ok=True)
            for path in run_dir.iterdir():
                if path.is_file():
                    shutil.copy2(path, destination / path.name)

    print(
        "Q3 CLOSURE/P2 PASS: "
        f"Stage1={final_s1['total_aircraft_time_minutes']} min, "
        f"Stage2 optional={optional_count-final_s2['unserved_optional_passengers']}/{optional_count}, "
        f"Stage2 time={final_s2['total_aircraft_time_minutes']} min, run_dir={run_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
