from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_json
from src.solver import load_problem_data
from src.solver.cache import SolverCache
from src.solver.data import ProblemData
from src.solver.q2 import build_q2_variant
from src.solver.q3 import (
    Q3Person,
    Q3Variant,
    _assignment_for_person,
    export_q3_schedule,
    load_q3_people,
    load_q3_schedule,
    load_q3_variants,
    optimize_fixed_flight_assignments,
    schedule_metrics,
    shorten_fixed_flight_routes,
)
from src.solver.q3_closure_p2 import build_mandatory_schedule_flexible_regret
from src.validation import validate_solution


def rebuild_variants(
    original: dict[tuple[str, str], tuple[Q3Variant, ...]],
    people: dict[str, Q3Person],
    data: ProblemData,
) -> tuple[dict[tuple[str, str], tuple[Q3Variant, ...]], dict[str, object]]:
    started = time.perf_counter()
    cache: dict[tuple[str, str, tuple[str, ...]], Q3Variant | None] = {}
    solver_cache = SolverCache(data)
    result = {}
    failed = 0
    for od, variants in original.items():
        representative = next(person for person in people.values() if person.od == od)
        values = {}
        for variant in variants:
            source = variant.source
            key = (source.base_airport, source.aircraft_type, source.service_order)
            if key not in cache:
                rebuilt = build_q2_variant(
                    data, key[0], key[1], key[2], cache=solver_cache
                )
                cache[key] = Q3Variant(rebuilt) if rebuilt is not None else None
                failed += rebuilt is None
            rebuilt_variant = cache[key]
            if rebuilt_variant is None:
                continue
            if _assignment_for_person(representative, rebuilt_variant, data.config) is None:
                continue
            values[rebuilt_variant.key] = rebuilt_variant
        result[od] = tuple(values.values())
    empty_ods = [list(od) for od, values in result.items() if not values]
    return result, {
        "source_templates": len(cache),
        "rebuilt_variants": len({variant.key for values in result.values() for variant in values}),
        "failed_templates": failed,
        "empty_ods": empty_ods,
        "runtime_seconds": round(time.perf_counter() - started, 6),
    }


def validate_export(name, flights, people, data, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    routes = directory / f"{name}-routes.csv"
    assignments = directory / f"{name}-assignments.csv"
    export_q3_schedule(flights, people, routes, assignments, data.config)
    result = validate_solution(
        "q3", routes, assignments, data_dir=ROOT / "data/raw", config=data.config
    )
    write_json(directory / f"{name}-validator.json", result.to_dict())
    return result


def custom_window_check(schedule, people):
    violations = []
    for flight in schedule:
        for person_id, (pickup, delivery) in flight.assignment_intervals.items():
            person = people[person_id]
            if flight.departures[pickup] < person.earliest or flight.arrivals[delivery] > person.latest:
                violations.append(person_id)
    return sorted(set(violations))


def run_scenario(name, people, variants, data, directory: Path, seed: int):
    started = time.perf_counter()
    if any(not values for values in variants.values()):
        return {
            "scenario": name,
            "status": "route-universe-incomplete",
            "empty_ods": [list(od) for od, values in variants.items() if not values],
        }
    stage1, constructor = build_mandatory_schedule_flexible_regret(
        people,
        variants,
        data,
        seed=seed,
        fleet_slot_policy="least_fragmentation",
    )
    if not constructor.get("feasible"):
        return {
            "scenario": name,
            "status": "no-feasible-reoptimization-found",
            "constructor": constructor,
            "runtime_seconds": round(time.perf_counter() - started, 6),
            "global_infeasibility_proof": False,
        }
    stage1, shorten = shorten_fixed_flight_routes(stage1, people, variants, data.config)
    try:
        stage2, unserved, assignment = optimize_fixed_flight_assignments(
            stage1, people, data.config, time_limit_seconds=60.0
        )
    except RuntimeError as exc:
        stage2, unserved, assignment = stage1, [], {"error": str(exc)}
    internal_window_violations = custom_window_check(stage2, people)
    official_stage1 = validate_export("q3-base", stage1, people, data, directory)
    official_stage2 = validate_export("q3", stage2, people, data, directory)
    return {
        "scenario": name,
        "status": "feasible" if not internal_window_violations else "internal-window-violation",
        "stage1": schedule_metrics(stage1, people),
        "stage2": schedule_metrics(stage2, people),
        "unserved": unserved,
        "constructor": constructor,
        "shorten": shorten,
        "assignment": assignment,
        "internal_window_violations": internal_window_violations,
        "official_validator": {
            "stage1_valid": official_stage1.valid,
            "stage1_issues": len(official_stage1.issues),
            "stage2_valid": official_stage2.valid,
            "stage2_issues": len(official_stage2.issues),
            "note": "official raw task windows; internal check additionally enforces scenario-tightened windows",
        },
        "runtime_seconds": round(time.perf_counter() - started, 6),
        "global_optimality_proof": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Q3 PRO V2 robustness reoptimization")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs/q3/q3-pro-v2")
    parser.add_argument("--source", type=Path, default=ROOT / "outputs/q3/q3-pro-v2/current_incumbent")
    parser.add_argument("--seed", type=int, default=20260819)
    args = parser.parse_args()

    output = args.output_root / "runs/robustness"
    output.mkdir(parents=True, exist_ok=True)
    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    original_variants = load_q3_variants(
        ROOT / "outputs/q2/pair_n3_h10.pkl", people.values(), data.config
    )
    incumbent = load_q3_schedule(
        args.source / "q3-base-routes.csv",
        args.source / "q3-base-assignments.csv",
        people,
        original_variants,
        data.config,
    )
    busiest = Counter(
        flight.aircraft_id for flight in incumbent for _ in range(flight.duration)
    ).most_common(1)[0][0]
    busy_base, busy_type, _tail = busiest.split("-")
    used_refuel = Counter(
        stop.facility_id
        for flight in incumbent
        for stop in flight.variant.source.route.stops
        if stop.refuel
    )
    unavailable_refuel = used_refuel.most_common(1)[0][0]

    scenarios = []
    for turnaround in (40, 45):
        config = replace(data.config, turnaround_minutes=turnaround)
        scenarios.append((f"turnaround-{turnaround}", people, config, "same"))

    fleet = {base: dict(values) for base, values in data.config.fleet_counts.items()}
    fleet[busy_base][busy_type] -= 1
    scenarios.append(
        (
            f"one-aircraft-unavailable-{busy_base}-{busy_type}",
            people,
            replace(data.config, fleet_counts=fleet),
            "same",
        )
    )
    scenarios.append(
        (
            f"refuel-unavailable-{unavailable_refuel}",
            people,
            replace(
                data.config,
                refuel_facilities=frozenset(
                    value for value in data.config.refuel_facilities if value != unavailable_refuel
                ),
            ),
            "rebuild",
        )
    )
    for factor in (1.05, 1.10):
        aircraft = {
            key: replace(value, speed_kmh=value.speed_kmh / factor)
            for key, value in data.config.aircraft_types.items()
        }
        scenarios.append(
            (f"flight-time-plus-{round((factor - 1) * 100)}pct", people, replace(data.config, aircraft_types=aircraft), "rebuild")
        )
    tightened = {
        pid: replace(person, earliest=person.earliest + 30, latest=person.latest - 30)
        for pid, person in people.items()
    }
    scenarios.append(("time-windows-tightened-30min", tightened, data.config, "same"))

    records = []
    for index, (name, scenario_people, config, rebuild) in enumerate(scenarios):
        scenario_data = ProblemData(
            config=config,
            matrix=data.matrix,
            q1_pools=data.q1_pools,
            q2_pools=data.q2_pools,
        )
        if rebuild == "rebuild" or config != data.config:
            scenario_variants, variant_report = rebuild_variants(
                original_variants, scenario_people, scenario_data
            )
        else:
            scenario_variants, variant_report = original_variants, {
                "source_templates": 3116,
                "rebuilt_variants": 3116,
                "runtime_seconds": 0.0,
            }
        record = run_scenario(
            name,
            scenario_people,
            scenario_variants,
            scenario_data,
            output / name,
            args.seed + index,
        )
        record["variant_rebuild"] = variant_report
        records.append(record)
        write_json(output / name / "result.json", record)
        write_json(args.output_root / "checkpoints/robustness.json", {"completed": index + 1, "records": records})
        print(f"ROBUSTNESS {index + 1}/{len(scenarios)} {name}: {record['status']}", flush=True)

    optional = [person for person in people.values() if not person.mandatory]
    reduced_optional = set(person.person_id for person in sorted(optional, key=lambda p: p.person_id)[:16])
    perturbed_people = {
        pid: person for pid, person in people.items() if pid not in reduced_optional
    }
    perturbed_stage2, unserved, stats = optimize_fixed_flight_assignments(
        incumbent, perturbed_people, data.config, time_limit_seconds=60.0
    )
    demand_record = {
        "scenario": "temporary-demand-minus-10pct",
        "status": "feasible-fixed-structure",
        "stage2": schedule_metrics(perturbed_stage2, perturbed_people),
        "removed_optional_ids": sorted(reduced_optional),
        "unserved": unserved,
        "assignment": stats,
        "scope": "demand perturbation on the final structure; no global optimality claim",
    }
    records.append(demand_record)
    write_json(output / "temporary-demand-minus-10pct/result.json", demand_record)
    summary = {
        "incumbent": schedule_metrics(incumbent, people),
        "busiest_aircraft_type_removed": f"{busy_base}-{busy_type}",
        "refuel_facility_removed": unavailable_refuel,
        "records": records,
        "interpretation": (
            "Each structural scenario attempts full schedule reconstruction from the rebuilt "
            "route universe. A failed constructor is evidence of search failure, not a global "
            "infeasibility certificate."
        ),
    }
    write_json(args.output_root / "robustness-v2.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
