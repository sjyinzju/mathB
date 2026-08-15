from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_csv, write_json
from src.solver import load_problem_data
from src.solver.q3 import load_q3_people, transport_time_lower_bound
from src.validation import validate_solution


ASSIGNMENT_FIELDS = [
    "person_id",
    "aircraft_id",
    "flight_no",
    "pickup_stop_order",
    "delivery_stop_order",
]


def _read(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def main() -> int:
    parser = argparse.ArgumentParser(description="固化并校验问题三深度优化结果")
    parser.add_argument("--base-routes", type=Path, required=True)
    parser.add_argument("--base-assignments", type=Path, required=True)
    parser.add_argument("--final-routes", type=Path, required=True)
    parser.add_argument("--final-assignments", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    mandatory = [person for person in people.values() if person.mandatory]
    optional_count = sum(not person.mandatory for person in people.values())

    base_routes = args.output_dir / "q3-base-routes.csv"
    base_assignments = args.output_dir / "q3-base-assignments.csv"
    final_routes = args.output_dir / "q3-routes.csv"
    final_assignments = args.output_dir / "q3-assignments.csv"
    shutil.copy2(args.base_routes, base_routes)
    shutil.copy2(args.final_routes, final_routes)
    shutil.copy2(args.final_assignments, final_assignments)

    base_rows = _read(args.base_assignments)
    for row in base_rows:
        if not people[row["person_id"]].mandatory:
            for field in ASSIGNMENT_FIELDS[1:]:
                row[field] = ""
    write_csv(base_assignments, ASSIGNMENT_FIELDS, base_rows)

    base_validation = validate_solution(
        "q3", base_routes, base_assignments, data_dir=ROOT / "data/raw", config=data.config
    )
    final_validation = validate_solution(
        "q3", final_routes, final_assignments, data_dir=ROOT / "data/raw", config=data.config
    )
    if not base_validation.valid or not final_validation.valid:
        raise RuntimeError(
            "Q3 checkpoint validation failed: "
            f"base={base_validation.issues[:5]}, final={final_validation.issues[:5]}"
        )
    assert base_validation.metrics is not None and final_validation.metrics is not None
    base_metrics = base_validation.metrics.to_dict()
    final_metrics = final_validation.metrics.to_dict()
    served_optional = optional_count - int(final_metrics["unserved_optional_passengers"])
    if int(final_metrics["total_aircraft_time_minutes"]) > int(
        base_metrics["total_aircraft_time_minutes"]
    ):
        raise RuntimeError("Final result violates the stage-one T0 upper bound")

    lower_bound = transport_time_lower_bound(mandatory, data)
    write_json(args.output_dir / "q3-base-validator.json", base_validation.to_dict())
    write_json(args.output_dir / "q3-validator.json", final_validation.to_dict())
    write_json(
        args.output_dir / "q3-bounds.json",
        {
            "stage1": {
                "incumbent_aircraft_time_minutes": base_metrics[
                    "total_aircraft_time_minutes"
                ],
                "seat_km_transport_lower_bound_minutes": lower_bound,
                "conservative_gap_percent": round(
                    100
                    * (base_metrics["total_aircraft_time_minutes"] - lower_bound)
                    / base_metrics["total_aircraft_time_minutes"],
                    6,
                ),
                "incumbent_excess_over_lower_bound_percent": round(
                    100
                    * (base_metrics["total_aircraft_time_minutes"] - lower_bound)
                    / lower_bound,
                    6,
                ),
            },
            "stage2": {
                "served_optional_incumbent": served_optional,
                "optional_upper_bound": optional_count,
                "absolute_gap": optional_count - served_optional,
                "proven_optimal": served_optional == optional_count,
                "aircraft_time_slack_minutes": int(
                    base_metrics["total_aircraft_time_minutes"]
                )
                - int(final_metrics["total_aircraft_time_minutes"]),
            },
        },
    )
    write_json(
        args.output_dir / "metrics.json",
        {
            "gate_pass": True,
            "method": "guided_route_pool_alternating_assignment_destroy_repair",
            "mandatory_count": len(mandatory),
            "optional_count": optional_count,
            "served_optional": served_optional,
            "baseline_metrics": base_metrics,
            "final_metrics": final_metrics,
            "improvement_from_v5": {
                "baseline_aircraft_time_saved_minutes": 31371
                - int(base_metrics["total_aircraft_time_minutes"]),
                "final_aircraft_time_saved_minutes": 31371
                - int(final_metrics["total_aircraft_time_minutes"]),
                "additional_optional_people": served_optional - 158,
            },
        },
    )
    write_json(
        args.output_dir / "q3-optimization-summary.json",
        {
            "initial_v5": {"T0": 31371, "temporary": 158, "final_time": 31371},
            "deep_stage1": {
                "T0": base_metrics["total_aircraft_time_minutes"],
                "temporary": 0,
            },
            "deep_stage2": {
                "final_time": final_metrics["total_aircraft_time_minutes"],
                "temporary": served_optional,
                "proven_optimal_temporary_count": served_optional == optional_count,
            },
            "mechanisms": [
                "guided bottleneck OD construction",
                "global sparse binary passenger reassignment",
                "same-type route shortening",
                "aircraft type and home-airport local search",
                "stop deletion plus global assignment repair",
            ],
        },
    )

    if args.promote:
        best = ROOT / "outputs/q3/best"
        best.mkdir(parents=True, exist_ok=True)
        for path in args.output_dir.iterdir():
            if path.is_file():
                shutil.copy2(path, best / path.name)
    print(
        "Q3 FINAL PASS: "
        f"T0={base_metrics['total_aircraft_time_minutes']} min, "
        f"final={final_metrics['total_aircraft_time_minutes']} min, "
        f"temporary={served_optional}/{optional_count}, LB={lower_bound} min"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
