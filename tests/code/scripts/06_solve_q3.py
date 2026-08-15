from __future__ import annotations

import argparse
import shutil
import sys
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_csv, write_json
from src.solver import load_problem_data
from src.solver.q3 import (
    build_mandatory_schedule,
    destroy_repair_route_descent,
    export_q3_schedule,
    insert_optional_people,
    load_q3_people,
    load_q3_variants,
    optimize_fixed_flight_assignments,
    retype_and_rehome_flights,
    schedule_task_counts,
    shorten_fixed_flight_routes,
    transport_time_lower_bound,
)
from src.validation import validate_solution


def _passenger_time(flights) -> int:
    return sum(
        flight.variant.source.arrivals[delivery]
        - flight.variant.source.departures[pickup]
        for flight in flights
        for pickup, delivery in flight.assignment_intervals.values()
    )


def _comparison_key(flights) -> tuple[float, ...]:
    return (
        float(sum(flight.variant.duration for flight in flights)),
        float(_passenger_time(flights)),
        float(len(flights)),
        float(sum(flight.variant.fuel_kg for flight in flights)),
    )


def _mandatory_only(flights, people):
    result = deepcopy(list(flights))
    for flight in result:
        optional_ids = [
            person_id
            for person_id in flight.person_ids
            if not people[person_id].mandatory
        ]
        for person_id in optional_ids:
            flight.person_ids.remove(person_id)
            flight.assignment_intervals.pop(person_id, None)
    return result


def _alternating_polish(flights, people, variants, data, *, rounds, milp_time):
    work = deepcopy(list(flights))
    history = []
    unserved = []
    milp_stats = {}
    for round_index in range(rounds):
        work, unserved, milp_stats = optimize_fixed_flight_assignments(
            work, people, data.config, time_limit_seconds=milp_time
        )
        work, short_stats = shorten_fixed_flight_routes(
            work, people, variants, data.config
        )
        work, type_stats = retype_and_rehome_flights(
            work, people, variants, data.config, maximum_passes=3
        )
        history.append(
            {
                "round": round_index + 1,
                "served_optional": sum(
                    not people[person_id].mandatory
                    for flight in work
                    for person_id in flight.person_ids
                ),
                "aircraft_time_minutes": sum(
                    flight.variant.duration for flight in work
                ),
                "same_type_shortening": short_stats,
                "aircraft_type_and_home_search": type_stats,
            }
        )
    work, unserved, milp_stats = optimize_fixed_flight_assignments(
        work, people, data.config, time_limit_seconds=milp_time
    )
    return work, unserved, milp_stats, history


def main() -> int:
    parser = argparse.ArgumentParser(description="问题三时间窗与有限机队两阶段求解器")
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs/q3")
    parser.add_argument(
        "--variant-cache",
        type=Path,
        default=ROOT / "outputs/q2/pair_n3_h10.pkl",
    )
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--assignment-milp-time", type=float, default=90.0)
    parser.add_argument(
        "--deep",
        action="store_true",
        help="启用机型/属地调整与删停靠—全局重分配深度邻域",
    )
    parser.add_argument("--polish-rounds", type=int, default=3)
    parser.add_argument("--stage1-destroy-trials", type=int, default=140)
    parser.add_argument("--stage2-destroy-trials", type=int, default=340)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-q3"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"运行目录已存在：{run_dir}")
    run_dir.mkdir(parents=True)
    started = time.perf_counter()

    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    variants = load_q3_variants(args.variant_cache, people.values(), data.config)
    mandatory_people = [person for person in people.values() if person.mandatory]
    optional_people = [person for person in people.values() if not person.mandatory]
    lower_bound = transport_time_lower_bound(mandatory_people, data)

    experiments = []
    feasible_runs = []
    modes = ("priority", "deadline", "slack")
    for index in range(args.seeds):
        mode = modes[index % len(modes)]
        flights, stats = build_mandatory_schedule(
            people,
            variants,
            data,
            mode=mode,
            seed=index,
        )
        record = stats.to_dict()
        record["passenger_time_minutes"] = _passenger_time(flights)
        record["fuel_kg"] = round(sum(flight.variant.fuel_kg for flight in flights), 6)
        experiments.append(record)
        if stats.feasible:
            feasible_runs.append((flights, stats))
    if not feasible_runs:
        write_json(run_dir / "q3-experiments.json", experiments)
        raise RuntimeError("No Q3 mandatory schedule was feasible; inspect q3-experiments.json")

    baseline, baseline_stats = min(
        feasible_runs,
        key=lambda item: (_comparison_key(item[0]), item[1].runtime_seconds),
    )
    optimization_trace: dict[str, object] = {"deep": bool(args.deep)}
    if args.deep:
        stage1_work, stage1_unserved, stage1_milp, stage1_polish = (
            _alternating_polish(
                baseline,
                people,
                variants,
                data,
                rounds=args.polish_rounds,
                milp_time=args.assignment_milp_time,
            )
        )
        stage1_work, stage1_descent = destroy_repair_route_descent(
            stage1_work,
            people,
            variants,
            data.config,
            minimum_optional_served=0,
            maximum_trials=args.stage1_destroy_trials,
            assignment_time_limit_seconds=args.assignment_milp_time,
        )
        baseline = _mandatory_only(stage1_work, people)

        guide_ids = stage1_unserved or ["P1102", "P3290"]
        guided, guided_stats = build_mandatory_schedule(
            people,
            variants,
            data,
            mode="deadline",
            seed=11,
            guide_optional_ids=guide_ids,
        )
        if not guided_stats.feasible:
            raise RuntimeError("Q3 guided stage-2 construction was infeasible")
        final, unserved_optional, assignment_milp, stage2_polish = (
            _alternating_polish(
                guided,
                people,
                variants,
                data,
                rounds=args.polish_rounds,
                milp_time=args.assignment_milp_time,
            )
        )
        final, stage2_descent = destroy_repair_route_descent(
            final,
            people,
            variants,
            data.config,
            minimum_optional_served=len(optional_people),
            maximum_trials=args.stage2_destroy_trials,
            assignment_time_limit_seconds=args.assignment_milp_time,
        )
        final, unserved_optional, assignment_milp = optimize_fixed_flight_assignments(
            final,
            people,
            data.config,
            time_limit_seconds=args.assignment_milp_time,
        )
        optimization_trace.update(
            {
                "stage1_polish": stage1_polish,
                "stage1_destroy_repair": stage1_descent,
                "stage1_assignment_milp": stage1_milp,
                "stage2_guide_ids": guide_ids,
                "stage2_guided_constructor": guided_stats.to_dict(),
                "stage2_polish": stage2_polish,
                "stage2_destroy_repair": stage2_descent,
            }
        )
    base_routes = run_dir / "q3-base-routes.csv"
    base_assignments = run_dir / "q3-base-assignments.csv"
    export_q3_schedule(baseline, people, base_routes, base_assignments, data.config)
    base_validation = validate_solution(
        "q3",
        base_routes,
        base_assignments,
        data_dir=ROOT / "data/raw",
        config=data.config,
    )
    write_json(run_dir / "q3-base-validator.json", base_validation.to_dict())
    if not base_validation.valid:
        raise RuntimeError(
            "Q3 baseline failed validation: "
            + "; ".join(str(issue) for issue in base_validation.issues[:8])
        )

    heuristic_final, heuristic_unserved = insert_optional_people(
        baseline, people, data.config
    )
    if not args.deep:
        final, unserved_optional, assignment_milp = optimize_fixed_flight_assignments(
            baseline,
            people,
            data.config,
            time_limit_seconds=args.assignment_milp_time,
        )
    final_routes = run_dir / "q3-routes.csv"
    final_assignments = run_dir / "q3-assignments.csv"
    export_q3_schedule(final, people, final_routes, final_assignments, data.config)
    final_validation = validate_solution(
        "q3",
        final_routes,
        final_assignments,
        data_dir=ROOT / "data/raw",
        config=data.config,
    )
    write_json(run_dir / "q3-validator.json", final_validation.to_dict())
    if not final_validation.valid:
        raise RuntimeError(
            "Q3 final schedule failed validation: "
            + "; ".join(str(issue) for issue in final_validation.issues[:8])
        )

    base_metrics = base_validation.metrics.to_dict() if base_validation.metrics else None
    final_metrics = final_validation.metrics.to_dict() if final_validation.metrics else None
    served_optional = len(optional_people) - len(unserved_optional)
    if final_metrics and int(final_metrics["total_aircraft_time_minutes"]) > int(
        base_metrics["total_aircraft_time_minutes"]
    ):
        raise RuntimeError("Q3 stage 2 exceeded the stage-1 aircraft-time upper bound")

    write_json(
        run_dir / "q3-bounds.json",
        {
            "stage1": {
                "incumbent_aircraft_time_minutes": base_metrics[
                    "total_aircraft_time_minutes"
                ],
                "seat_km_transport_lower_bound_minutes": lower_bound,
                "conservative_gap_percent": round(
                    100.0
                    * (base_metrics["total_aircraft_time_minutes"] - lower_bound)
                    / base_metrics["total_aircraft_time_minutes"],
                    6,
                ),
                "incumbent_excess_over_lower_bound_percent": round(
                    100.0
                    * (base_metrics["total_aircraft_time_minutes"] - lower_bound)
                    / lower_bound,
                    6,
                ),
                "scope": (
                    "The lower bound ignores return legs, stops, fuel detours, time windows "
                    "and fleet conflicts, so it is valid for the original problem but may be weak."
                ),
            },
            "stage2": {
                "served_optional_incumbent": served_optional,
                "optional_upper_bound": len(optional_people),
                "absolute_gap": len(optional_people) - served_optional,
                "proven_optimal": served_optional == len(optional_people),
            },
        },
    )
    write_json(
        run_dir / "metrics.json",
        {
            "gate_pass": bool(base_validation.valid and final_validation.valid),
            "mandatory_count": len(mandatory_people),
            "optional_count": len(optional_people),
            "served_optional": served_optional,
            "unserved_optional": unserved_optional,
            "heuristic_served_optional": len(optional_people) - len(heuristic_unserved),
            "assignment_milp": assignment_milp,
            "baseline_metrics": base_metrics,
            "final_metrics": final_metrics,
            "baseline_task_counts": schedule_task_counts(baseline, people),
            "final_task_counts": schedule_task_counts(final, people),
            "selected_run": baseline_stats.to_dict(),
            "optimization_trace": optimization_trace,
        },
    )
    write_json(run_dir / "q3-experiments.json", experiments)
    write_csv(
        run_dir / "q3-stability.csv",
        [
            "method",
            "seed",
            "feasible",
            "mandatory_people",
            "aircraft_time_minutes",
            "passenger_time_minutes",
            "flights",
            "fuel_kg",
            "lower_bound_minutes",
            "lower_bound_gap_percent",
            "runtime_seconds",
            "conflict_count",
        ],
        experiments,
    )
    write_json(
        run_dir / "run_config.json",
        {
            "method": (
                "q3_deep_destroy_repair_interval_scheduling"
                if args.deep
                else "q3_time_window_candidate_routes_interval_scheduling"
            ),
            "variant_cache": str(args.variant_cache),
            "od_count": len(variants),
            "candidate_variants": sum(len(values) for values in variants.values()),
            "seeds": args.seeds,
            "selected_mode": baseline_stats.method,
            "selected_seed": baseline_stats.seed,
            "total_elapsed_seconds": round(time.perf_counter() - started, 6),
        },
    )

    promoted = False
    if args.promote:
        best_dir = args.output_root / "best"
        best_dir.mkdir(parents=True, exist_ok=True)
        for path in run_dir.iterdir():
            if path.is_file():
                shutil.copy2(path, best_dir / path.name)
        promoted = True
    print(
        "Q3 PASS: "
        f"T0={base_metrics['total_aircraft_time_minutes']} min, "
        f"temporary={served_optional}/{len(optional_people)}, "
        f"final_time={final_metrics['total_aircraft_time_minutes']} min, "
        f"flights={final_metrics['total_flights']}, "
        f"LB={lower_bound} min, promoted={promoted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
