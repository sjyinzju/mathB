from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import asdict, replace
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
    retype_and_rehome_flights,
    schedule_metrics,
    shorten_fixed_flight_routes,
    stage1_key,
    stage2_key,
)
from src.solver.q3_bounds import (
    candidate_route_master_lp_bound,
    layered_multicommodity_flow_bound,
)
from src.solver.q3_pro import ElitePool, build_route_library, solution_distance
from src.solver.q3_pro_v2 import (
    OptionalRescueSolver,
    aggregate_convergence,
    aircraft_day_chain_search,
    atomic_write_json,
    build_flight_column_library,
    checkpoint_payload,
    guided_exact_lns,
    local_branching_search,
    optional_rescue_dossier_v2,
    parameter_grid,
    pricing_guided_variant_pool,
    recombine_elites,
    run_portfolio_config,
)
from src.validation import validate_solution


def repo_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def validate_export(name, flights, people, data, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
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
            raise RuntimeError(f"{name} memory/export mismatch for {key}")
    return result, routes, assignments


def load_schedule(directory: Path, prefix: str, people, variants, config):
    return load_q3_schedule(
        directory / f"{prefix}-routes.csv",
        directory / f"{prefix}-assignments.csv",
        people,
        variants,
        config,
    )


def collect_seed_schedules(people, variants, data, source_best: Path):
    seeds = [
        (
            "v2-baseline",
            load_q3_schedule(
                source_best / "q3-base-routes.csv",
                source_best / "q3-base-assignments.csv",
                people,
                variants,
                data.config,
            ),
        )
    ]
    diverse = ROOT / "outputs/q3/p0_p1_best"
    if (diverse / "q3-base-routes.csv").exists():
        seeds.append(
            (
                "p0-p1-diverse",
                load_q3_schedule(
                    diverse / "q3-base-routes.csv",
                    diverse / "q3-base-assignments.csv",
                    people,
                    variants,
                    data.config,
                ),
            )
        )
    v1_elites = ROOT / "outputs/q3/q3-pro/elite_pool/q3-pro-deep-v1"
    if v1_elites.exists():
        for directory in sorted(path for path in v1_elites.iterdir() if path.is_dir()):
            routes = directory / "q3-base-routes.csv"
            assignments = directory / "q3-base-assignments.csv"
            if routes.exists() and assignments.exists():
                seeds.append(
                    (
                        f"v1-elite:{directory.name}",
                        load_q3_schedule(routes, assignments, people, variants, data.config),
                    )
                )
    unique = {}
    for name, schedule in seeds:
        signature = tuple(
            sorted(
                (
                    repr(flight.variant.key),
                    flight.start // 1440,
                    tuple(sorted(flight.person_ids)),
                )
                for flight in schedule
            )
        )
        unique.setdefault(signature, (name, schedule))
    return list(unique.values())


def restricted_pool(variants_by_od, incumbent, per_od: int = 3):
    used = {flight.variant.key for flight in incumbent}
    result = {}
    for od, variants in variants_by_od.items():
        ranked = sorted(
            variants,
            key=lambda variant: (
                variant.key not in used,
                variant.duration / max(1, variant.capacity),
                variant.duration,
                variant.key,
            ),
        )
        result[od] = tuple(ranked[: max(per_od, sum(v.key in used for v in ranked))])
    return result


def screen_row(run_name: str, summary: dict[str, object]) -> dict[str, object]:
    config = summary["config"]
    return {
        "run": run_name,
        "config_id": config["config_id"],
        "seed": config["seed"],
        "operator_profile": config["operator_profile"],
        "iterations": summary["completed_iterations"],
        "final_aircraft_time": int(summary["final_key"][0]),
        "improvement": summary["best_improvement_minutes"],
        "runtime_seconds": summary["runtime_seconds"],
        "improvement_per_cpu_minute": summary["improvement_per_cpu_minute"],
        "accepted_rate": summary["accepted_rate"],
        "cross_day_attempts": summary["cross_day_attempts"],
        "cross_day_accepted": summary["cross_day_accepted"],
        "elite_size": summary["elite_size"],
        "elite_diversity": summary["elite_diversity"],
        "restart_threshold": config["restart_threshold"],
        "group_range": f"{config['normal_group_min']}-{config['normal_group_max']}",
        "heavy_group_range": f"{config['heavy_group_min']}-{config['heavy_group_max']}",
        "cross_day_trials": config["cross_day_trials"],
        "route_limit": config["route_limit"],
        "combination_budget": config["combination_budget"],
        "reaction_factor": config["reaction_factor"],
    }


def select_top_families(rows, summaries, count: int):
    ranked = sorted(
        rows,
        key=lambda row: (
            int(row["final_aircraft_time"]),
            -float(row["improvement_per_cpu_minute"]),
            -float(row["elite_diversity"]),
            str(row["run"]),
        ),
    )
    selected = []
    seen_profiles = set()
    for row in ranked:
        profile = row["operator_profile"]
        if profile not in seen_profiles:
            selected.append(summaries[str(row["run"])]["config"])
            seen_profiles.add(profile)
        if len(selected) == count:
            return selected
    for row in ranked:
        config = summaries[str(row["run"])]["config"]
        if config not in selected:
            selected.append(config)
        if len(selected) == count:
            break
    return selected


def persist_elite_pool(pool, people, data, root: Path):
    root.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index, record in enumerate(pool.records):
        directory = root / f"elite-{index:03d}"
        result, routes, assignments = validate_export("solution", record.flights, people, data, directory)
        record.validator_status = "valid, zero issues"
        manifest.append(
            {
                "index": index,
                "source": record.source,
                "seed": record.seed,
                "objective": list(record.objective),
                "signature": record.signature,
                "metrics": result.metrics.to_dict(),
                "routes": str(routes.relative_to(root.parent.parent)),
                "assignments": str(assignments.relative_to(root.parent.parent)),
                "validator": "valid, zero issues",
            }
        )
    write_json(root / "manifest.json", {"summary": pool.summary(), "records": manifest})
    return manifest


def write_convergence(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    write_csv(path, list(rows[0]), rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Q3 PRO V2 deep portfolio optimizer")
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs/q3/q3-pro-v2")
    parser.add_argument("--source-best", type=Path, default=ROOT / "outputs/q3/q3-pro-v2/current_incumbent")
    parser.add_argument("--variant-cache", type=Path, default=ROOT / "outputs/q2/pair_n3_h10.pkl")
    parser.add_argument("--master-seed", type=int, default=20260818)
    parser.add_argument("--screen-configs", type=int, default=20)
    parser.add_argument("--screen-seeds", type=int, default=2)
    parser.add_argument("--screen-iterations", type=int, default=100)
    parser.add_argument("--screen-wall-time", type=float, default=300.0)
    parser.add_argument("--deep-islands", type=int, default=4)
    parser.add_argument("--deep-iterations", type=int, default=1200)
    parser.add_argument("--deep-wall-time", type=float, default=1800.0)
    parser.add_argument("--optional-trials", type=int, default=30)
    parser.add_argument("--exact-windows", type=int, default=50)
    parser.add_argument("--exact-time-limit", type=float, default=20.0)
    parser.add_argument("--aircraft-day-windows", type=int, default=20)
    parser.add_argument("--recombination-pairs", type=int, default=20)
    parser.add_argument("--pricing-iterations", type=int, default=120)
    parser.add_argument("--run-global-bound", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q3-pro-v2"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists() and not args.resume:
        raise FileExistsError(run_dir)
    for path in (
        run_dir,
        args.output_root / "runs/screen",
        args.output_root / "runs/alns",
        args.output_root / "runs/optional_rescue",
        args.output_root / "runs/exact_lns",
        args.output_root / "runs/local_branching",
        args.output_root / "runs/pricing",
        args.output_root / "runs/recombination",
        args.output_root / "runs/p3",
        args.output_root / "checkpoints",
        args.output_root / "elite_pool/v2-final",
        args.output_root / "route_library",
        args.output_root / "column_library",
        args.output_root / "reports",
    ):
        path.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    source_commit = repo_sha()

    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    mandatory_count = sum(person.mandatory for person in people.values())
    optional_count = sum(not person.mandatory for person in people.values())
    variants = load_q3_variants(args.variant_cache, people.values(), data.config)
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
    validate_export("baseline-stage1", baseline_stage1, people, data, run_dir)
    validate_export("baseline-stage2", baseline_stage2, people, data, run_dir)
    cap_baseline = int(schedule_metrics(baseline_stage1, people)["total_aircraft_time_minutes"])

    seeds = collect_seed_schedules(people, variants, data, args.source_best)
    global_pool = ElitePool(people, stage=1, maximum_size=50)
    for name, schedule in seeds:
        global_pool.add(schedule, source=name, seed=args.master_seed)

    configs = parameter_grid(args.master_seed, args.screen_configs)
    screen_rows = []
    screen_summaries = {}
    all_traces = []
    for config_index, base_config in enumerate(configs):
        for repeat in range(args.screen_seeds):
            config = replace(
                base_config,
                config_id=f"{base_config.config_id}-s{repeat}",
                seed=base_config.seed + repeat * 1_000_003,
            )
            name = config.config_id
            directory = args.output_root / "runs/screen" / name
            if args.resume and (directory / "candidate-routes.csv").exists():
                candidate = load_schedule(directory, "candidate", people, variants, data.config)
                summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
                local_pool = ElitePool(people, stage=1, maximum_size=50)
                local_pool.add(candidate, source=f"resume:{name}", seed=config.seed)
            else:
                candidate, local_pool, summary = run_portfolio_config(
                    config,
                    seeds,
                    people,
                    variants,
                    data,
                    iterations=args.screen_iterations,
                    wall_time_seconds=args.screen_wall_time,
                    elite_size=50,
                )
                validate_export("candidate", candidate, people, data, directory)
                write_json(directory / "summary.json", summary)
            for record in local_pool.records:
                global_pool.add(record.flights, source=f"screen:{name}:{record.source}", seed=config.seed)
            global_pool.add(candidate, source=f"screen-final:{name}", seed=config.seed)
            row = screen_row(name, summary)
            screen_rows.append(row)
            screen_summaries[name] = summary
            all_traces.append(summary)
            atomic_write_json(
                args.output_root / "checkpoints/screen.json",
                checkpoint_payload(
                    phase="screen",
                    completed=len(screen_rows),
                    best=global_pool.best.flights,
                    people=people,
                    elite=global_pool,
                    route_library_version="v1-3116",
                    column_library_version="pending",
                ),
            )
            print(
                f"SCREEN {len(screen_rows)}/{len(configs) * args.screen_seeds}: "
                f"{name} -> {int(row['final_aircraft_time'])}, elites={len(global_pool.records)}",
                flush=True,
            )
    write_csv(args.output_root / "parameter-screen.csv", list(screen_rows[0]), screen_rows)
    write_json(
        args.output_root / "parameter-screen-summary.json",
        {
            "runs": screen_summaries,
            "selected_families": select_top_families(
                screen_rows, screen_summaries, args.deep_islands
            ),
        },
    )

    selected = select_top_families(screen_rows, screen_summaries, args.deep_islands)
    deep_rows = []
    for island_index, raw in enumerate(selected):
        config = next(row for row in configs if row.config_id == str(raw["config_id"]).split("-s")[0])
        config = replace(
            config,
            config_id=f"island-{island_index:02d}-{config.operator_profile}",
            seed=int(raw["seed"]) + 7_000_001,
        )
        directory = args.output_root / "runs/alns" / config.config_id
        exchange_seeds = [
            (f"exchange:{index}:{record.source}", record.flights)
            for index, record in enumerate(global_pool.records[: min(12, len(global_pool.records))])
        ]
        candidate, local_pool, summary = run_portfolio_config(
            config,
            exchange_seeds,
            people,
            variants,
            data,
            iterations=args.deep_iterations,
            wall_time_seconds=args.deep_wall_time,
            elite_size=50,
        )
        validate_export("candidate", candidate, people, data, directory)
        write_json(directory / "summary.json", summary)
        for record in local_pool.records:
            global_pool.add(record.flights, source=f"island:{island_index}:{record.source}", seed=config.seed)
        global_pool.add(candidate, source=f"island-final:{island_index}", seed=config.seed)
        deep_rows.append(screen_row(config.config_id, summary))
        all_traces.append(summary)
        atomic_write_json(
            args.output_root / f"checkpoints/island-{island_index:02d}.json",
            checkpoint_payload(
                phase="deep-islands",
                completed=island_index + 1,
                best=global_pool.best.flights,
                people=people,
                elite=global_pool,
                route_library_version="v1-3116",
                column_library_version="pending",
            ),
        )
        print(
            f"ISLAND {island_index + 1}/{len(selected)} -> "
            f"{int(stage1_key(global_pool.best.flights, people)[0])}, elites={len(global_pool.records)}",
            flush=True,
        )
    if deep_rows:
        write_csv(args.output_root / "deep-islands.csv", list(deep_rows[0]), deep_rows)

    stage1 = deepcopy(global_pool.best.flights)
    restricted = candidate_route_master_lp_bound(
        people.values(), restricted_pool(variants, stage1, 3), data, time_limit_seconds=120.0
    )
    full_pool = candidate_route_master_lp_bound(
        people.values(), variants, data, time_limit_seconds=300.0
    )
    pricing = {
        "restricted_master": restricted.to_dict(),
        "priced_full_pool_master": full_pool.to_dict(),
        "pricing_columns_added": int(full_pool.details["route_variants"])
        - int(restricted.details["route_variants"]),
        "interpretation": "finite-pool LP diagnostics only",
    }
    guided_variants, import_report = pricing_guided_variant_pool(variants, pricing, per_od=10)
    pricing["primal_import"] = import_report
    pricing_config = replace(
        configs[0],
        config_id="pricing-to-primal",
        seed=args.master_seed + 88_000_001,
        operator_profile="diverse",
        route_limit=240,
        heavy_route_limit=420,
    )
    pricing_candidate, pricing_pool, pricing_summary = run_portfolio_config(
        pricing_config,
        [("current-best", stage1)],
        people,
        guided_variants,
        data,
        iterations=args.pricing_iterations,
        wall_time_seconds=max(120.0, args.screen_wall_time),
        elite_size=50,
    )
    validate_export(
        "candidate", pricing_candidate, people, data, args.output_root / "runs/pricing/primal-import"
    )
    pricing["primal_search"] = pricing_summary
    for record in pricing_pool.records:
        global_pool.add(record.flights, source=f"pricing:{record.source}", seed=pricing_config.seed)
    global_pool.add(pricing_candidate, source="pricing-final", seed=pricing_config.seed)
    stage1 = deepcopy(global_pool.best.flights)
    write_json(args.output_root / "runs/pricing/pricing-to-primal.json", pricing)

    recombined, recombination = recombine_elites(
        global_pool,
        people,
        data,
        pairs=args.recombination_pairs if len(global_pool.records) >= 15 else 0,
        assignment_time_limit=args.exact_time_limit,
    )
    if stage1_key(recombined, people) < stage1_key(stage1, people):
        stage1 = recombined
        global_pool.add(stage1, source="recombination-final", seed=args.master_seed)
    recombination["elite_gate"] = {
        "required": 15,
        "actual": len(global_pool.records),
        "relink_executed": len(global_pool.records) >= 15,
    }
    write_json(args.output_root / "runs/recombination/recombination.json", recombination)

    exact, exact_trace = guided_exact_lns(
        stage1,
        people,
        variants,
        data,
        windows=args.exact_windows,
        seed=args.master_seed + 99_000_001,
        assignment_time_limit=args.exact_time_limit,
    )
    if stage1_key(exact, people) < stage1_key(stage1, people):
        stage1 = exact
        global_pool.add(stage1, source="guided-exact-final", seed=args.master_seed)
    write_json(args.output_root / "runs/exact_lns/guided-exact.json", exact_trace)
    validate_export("candidate", stage1, people, data, args.output_root / "runs/exact_lns/final")

    local, local_trace = local_branching_search(
        stage1,
        people,
        variants,
        data,
        radii=(5, 10, 20, 40, 80),
        seed=args.master_seed + 101_000_001,
        assignment_time_limit=args.exact_time_limit,
    )
    if stage1_key(local, people) < stage1_key(stage1, people):
        stage1 = local
        global_pool.add(stage1, source="local-branching-final", seed=args.master_seed)
    write_json(args.output_root / "runs/local_branching/local-branching.json", local_trace)

    aircraft_day, aircraft_day_trace = aircraft_day_chain_search(
        stage1,
        people,
        variants,
        data,
        windows=args.aircraft_day_windows,
        seed=args.master_seed + 102_000_001,
        assignment_time_limit=args.exact_time_limit,
    )
    if stage1_key(aircraft_day, people) < stage1_key(stage1, people):
        stage1 = aircraft_day
        global_pool.add(stage1, source="aircraft-day-final", seed=args.master_seed)
    write_json(args.output_root / "runs/local_branching/aircraft-day-chain.json", aircraft_day_trace)

    cap = int(schedule_metrics(stage1, people)["total_aircraft_time_minutes"])
    fixed_stage2, _unserved, _fixed = optimize_fixed_flight_assignments(
        stage1, people, data.config, time_limit_seconds=max(30.0, args.exact_time_limit)
    )
    stage2_seed = fixed_stage2
    if int(schedule_metrics(baseline_stage2, people)["total_aircraft_time_minutes"]) <= cap:
        stage2_seed = min((fixed_stage2, baseline_stage2), key=lambda value: stage2_key(value, people))
    rescue_solver = OptionalRescueSolver(
        people,
        variants,
        data,
        cap=cap,
        seed=args.master_seed + 77_000_001,
        assignment_time_limit=args.exact_time_limit,
    )
    stage2, rescue = rescue_solver.run(stage2_seed, trials_per_level=args.optional_trials)
    validate_export("candidate", stage2, people, data, args.output_root / "runs/optional_rescue/final")
    write_json(args.output_root / "runs/optional_rescue/optional-rescue-dossier-v2.json", rescue)

    feedback_rows = []
    for round_index in range(3):
        projection = project_mandatory_only(stage2, people)
        projection, short = shorten_fixed_flight_routes(projection, people, variants, data.config)
        projection, retype = retype_and_rehome_flights(
            projection, people, variants, data.config, maximum_passes=2
        )
        improved = stage1_key(projection, people) < stage1_key(stage1, people)
        feedback_rows.append(
            {
                "round": round_index + 1,
                "improved": improved,
                "projection_metrics": schedule_metrics(projection, people),
                "shorten": short,
                "retype": retype,
            }
        )
        if not improved:
            break
        stage1 = projection
        global_pool.add(stage1, source=f"p0-feedback:{round_index}", seed=args.master_seed)
        cap = int(schedule_metrics(stage1, people)["total_aircraft_time_minutes"])
        fixed_stage2, _unserved, _fixed = optimize_fixed_flight_assignments(
            stage1, people, data.config, time_limit_seconds=max(30.0, args.exact_time_limit)
        )
        stage2, rescue_round = OptionalRescueSolver(
            people,
            variants,
            data,
            cap=cap,
            seed=args.master_seed + 77_100_001 + round_index,
            assignment_time_limit=args.exact_time_limit,
        ).run(fixed_stage2, trials_per_level=args.optional_trials)
        feedback_rows[-1]["rescue"] = rescue_round
    write_json(args.output_root / "final-feedback.json", feedback_rows)

    stage1, short = shorten_fixed_flight_routes(stage1, people, variants, data.config)
    stage1, retype = retype_and_rehome_flights(stage1, people, variants, data.config, maximum_passes=3)
    fixed_polish, _unserved, fixed_stats = optimize_fixed_flight_assignments(
        stage2, people, data.config, time_limit_seconds=max(60.0, args.exact_time_limit)
    )
    if stage2_key(fixed_polish, people) < stage2_key(stage2, people):
        stage2 = fixed_polish
    secondary = {"stage1_shorten": short, "stage1_retype": retype, "stage2_fixed": fixed_stats}
    write_json(args.output_root / "secondary-polish.json", secondary)

    stage1_result, s1_routes, s1_assignments = validate_export("q3-base", stage1, people, data, run_dir)
    stage2_result, s2_routes, s2_assignments = validate_export("q3", stage2, people, data, run_dir)
    stage1_metrics = stage1_result.metrics.to_dict()
    stage2_metrics = stage2_result.metrics.to_dict()
    served_optional = int(stage2_metrics["served_passengers"]) - mandatory_count
    if int(stage1_metrics["served_passengers"]) != mandatory_count:
        raise RuntimeError("final Stage 1 mandatory coverage mismatch")
    if int(stage2_metrics["total_aircraft_time_minutes"]) > int(stage1_metrics["total_aircraft_time_minutes"]):
        raise RuntimeError("final Stage 2 violates the strict Stage 1 cap")

    global_bound = None
    if args.run_global_bound:
        global_bound = layered_multicommodity_flow_bound(
            people.values(), data, time_limit_seconds=900.0
        ).to_dict()
    else:
        prior = json.loads((ROOT / "outputs/q3/q3-pro/runs/q3-pro-deep-v1/p3-rmp-pricing.json").read_text(encoding="utf-8"))
        global_bound = {**prior["global_bound"], "name": "carried-forward-validated-global-bound"}
    bounds = {
        "global_valid_lower_bound": global_bound,
        "restricted_master": restricted.to_dict(),
        "finite_pool_master": full_pool.to_dict(),
        "best_feasible_ub": int(stage1_metrics["total_aircraft_time_minutes"]),
        "certified_gap_percent": 100.0
        * (int(stage1_metrics["total_aircraft_time_minutes"]) - int(global_bound["objective_minutes_integer_ceiling"]))
        / int(stage1_metrics["total_aircraft_time_minutes"]),
        "optional_certificate": {
            "served": served_optional,
            "fixed_structure_optimal": rescue["fixed_assignment"].get("fixed_flight_optimal"),
            "unrestricted_160": "open" if served_optional < optional_count else "feasible",
            "unrestricted_159": "open" if served_optional < 159 else "feasible",
            "unrestricted_158": "open" if served_optional < 158 else "feasible",
        },
    }
    write_json(run_dir / "bounds.json", bounds)

    global_pool.add(stage1, source="final-stage1", seed=args.master_seed)
    elite_manifest = persist_elite_pool(
        global_pool, people, data, args.output_root / "elite_pool/v2-final"
    )
    route_state = build_route_library(
        variants,
        stage1,
        source=f"q3-pro-v2:{run_id}",
        path=args.output_root / "route_library/routes.json",
    )
    column_state = build_flight_column_library(
        [(record.source, record.flights) for record in global_pool.records],
        people,
        path=args.output_root / "column_library/columns.json",
    )
    dossier = optional_rescue_dossier_v2(stage2, people, variants, data)
    write_json(args.output_root / "optional-rescue-dossier-v2.json", dossier)

    convergence = aggregate_convergence(
        all_traces,
        baseline_ub=cap_baseline,
        stage2_optional=served_optional,
        global_lb=int(global_bound["objective_minutes_integer_ceiling"]),
        restricted_lp=float(full_pool.objective_minutes_continuous),
        route_count=int(route_state["route_count"]),
        column_count=int(column_state["column_count"]),
    )
    write_convergence(args.output_root / "convergence.csv", convergence)

    metrics = {
        "baseline_stage1": schedule_metrics(baseline_stage1, people),
        "baseline_stage2": schedule_metrics(baseline_stage2, people),
        "final_stage1": stage1_metrics,
        "final_stage2": stage2_metrics,
        "served_optional": served_optional,
        "unserved_optional_ids": [row["person_id"] for row in dossier["records"]],
        "runtime_seconds": round(time.perf_counter() - started, 6),
        "search_budget": {
            "screen_runs": len(screen_rows),
            "deep_islands": len(deep_rows),
            "screen_iterations": args.screen_iterations,
            "deep_iterations": args.deep_iterations,
            "exact_windows": args.exact_windows,
            "aircraft_day_windows": args.aircraft_day_windows,
            "optional_trials_per_level": args.optional_trials,
            "recombination_pairs": args.recombination_pairs,
            "pricing_iterations": args.pricing_iterations,
        },
        "elite": {"persistent": len(elite_manifest), "summary": global_pool.summary()},
        "route_library": route_state,
        "column_library": column_state,
    }
    write_json(run_dir / "metrics.json", metrics)
    run_config = {**vars(args), "run_id": run_id, "source_commit": source_commit}
    run_config = {key: str(value) if isinstance(value, Path) else value for key, value in run_config.items()}
    write_json(run_dir / "run_config.json", run_config)

    current = args.output_root / "current_incumbent"
    current.mkdir(parents=True, exist_ok=True)
    for source, name in (
        (s1_routes, "q3-base-routes.csv"),
        (s1_assignments, "q3-base-assignments.csv"),
        (s2_routes, "q3-routes.csv"),
        (s2_assignments, "q3-assignments.csv"),
        (run_dir / "q3-base-validator.json", "q3-base-validator.json"),
        (run_dir / "q3-validator.json", "q3-validator.json"),
        (run_dir / "metrics.json", "metrics.json"),
        (run_dir / "bounds.json", "bounds.json"),
        (run_dir / "run_config.json", "run_config.json"),
    ):
        shutil.copy2(source, current / name)
    print(
        f"Q3 PRO V2 PASS: Stage1={stage1_metrics['total_aircraft_time_minutes']}, "
        f"Stage2={served_optional}/{optional_count} @ {stage2_metrics['total_aircraft_time_minutes']}, "
        f"elites={len(elite_manifest)}, columns={column_state['column_count']}, run_dir={run_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
