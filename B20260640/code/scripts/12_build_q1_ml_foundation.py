from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_csv, write_json
from src.solver import load_problem_data


def main() -> int:
    parser = argparse.ArgumentParser(description="Build leakage-safe Q1 candidate-value ML foundation")
    parser.add_argument(
        "--targeted-dir",
        type=Path,
        default=ROOT / "outputs" / "q1" / "final-or" / "round2-targeted-neighborhoods",
    )
    parser.add_argument(
        "--master-dir",
        type=Path,
        default=ROOT / "outputs" / "q1" / "final-or" / "round2-target-88",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "outputs" / "q1" / "final-or" / "ml-data",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    data = load_problem_data()
    events = json.loads((args.targeted_dir / "candidate-events.json").read_text(encoding="utf-8"))
    logs = json.loads((args.targeted_dir / "search-log.json").read_text(encoding="utf-8"))
    audit = json.loads(
        (args.targeted_dir / "route-elimination-audit-initial.json").read_text(encoding="utf-8")
    )
    audit_by_index = {int(row["route_index"]): row for row in audit}
    lp = json.loads((args.master_dir / "lp-diagnostics.json").read_text(encoding="utf-8"))["expanded"]
    duals = lp["demand_duals"]
    pool = json.loads((args.master_dir / "route-pool.json").read_text(encoding="utf-8"))
    route_frequency = {}
    for route in pool["routes"]:
        key = tuple(route["ordered_service_nodes"])
        route_frequency[key] = max(route_frequency.get(key, 0), len(route["sources"]))
    facility_demand = Counter()
    for (origin, destination), demand in data.q1_pools.items():
        facility_demand[destination] += demand.quantity

    rows = []
    for event, log in zip(events, logs):
        geometry = event.get("geometry") or []
        facilities = sorted({facility for route in geometry for facility in route})
        pair_distances = [
            data.matrix[left][right]
            for index, left in enumerate(facilities)
            for right in facilities[index + 1 :]
        ]
        route_indices = [int(index) for index in log.get("route_indices", [])]
        deletion = [
            float(audit_by_index[index]["elimination_potential"])
            for index in route_indices
            if index in audit_by_index
        ]
        frequency = [
            route_frequency.get(tuple(route), 0) for route in geometry
        ]
        row = {
            "run_id": event["run_id"],
            "seed": event["seed"],
            "warm_start": event["warm_start"],
            "parent_solution": event["parent_solution"],
            "basin_lineage": event["basin_lineage"],
            "algorithm": event["algorithm"],
            "geometry": json.dumps(geometry, ensure_ascii=False, separators=(",", ":")),
            "route_time": json.dumps(event.get("route_time"), separators=(",", ":")),
            "route_slack": json.dumps(event.get("route_slack"), separators=(",", ":")),
            "route_utilization": json.dumps(event.get("route_utilization"), separators=(",", ":")),
            "aircraft_type": json.dumps(event.get("aircraft_type"), separators=(",", ":")),
            "airport": json.dumps(event.get("airport"), separators=(",", ":")),
            "facility_demand": sum(facility_demand[facility] for facility in facilities),
            "source_route_deletion_potential": max(deletion) if deletion else "",
            "destroy_size": event["destroy_size"],
            "operator": event["operator"],
            "current_objective": event["current_objective"],
            "stagnation": 0,
            "distance_relatedness": (
                sum(pair_distances) / len(pair_distances) if pair_distances else 0.0
            ),
            "context_score": "",
            "lp_dual_pricing_signal": max(
                (
                    abs(float(value))
                    for key, value in duals.items()
                    if key.split("->", 1)[1] in facilities
                ),
                default=0.0,
            ),
            "route_pool_frequency": max(frequency, default=0),
            "elite_cooccurrence": sum(frequency),
            "candidate_evaluated": event["candidate_evaluated"],
            "feasible": event["feasible"],
            "repair_selected": event["repair_selected"],
            "repair_accepted": event["repair_accepted"],
            "primary_improvement": event["primary_improvement"],
            "new_best": event["new_best"],
            "actual_delta_aircraft_time": event["actual_delta_aircraft_time"],
            "evaluation_cost": event["evaluation_cost"],
            "label": event["label"],
        }
        rows.append(row)
    fields = list(rows[0])
    write_csv(args.output_dir / "candidate_events.csv", fields, rows)
    labels = Counter(row["label"] for row in rows)
    write_json(
        args.output_dir / "feature_schema.json",
        {
            "target": "Candidate Value, not cluster membership",
            "leakage_rule": "Every feature is available before repair outcome.",
            "features": fields[6:25],
            "grouping_keys": ["run_id", "basin_lineage", "parent_solution"],
        },
    )
    write_json(
        args.output_dir / "label_schema.json",
        {
            "POSITIVE": "evaluated and accepted useful repair",
            "TRUE_NEGATIVE": "evaluated feasible repair that was not accepted",
            "CENSORED": "candidate not evaluated; never treat as negative",
            "INVALID": "evaluated but infeasible/invalid",
            "counts": dict(labels),
        },
    )
    write_csv(
        args.output_dir / "run_manifest.csv",
        ["run_id", "basin_lineage", "algorithm", "rows", "positive_rows"],
        [
            {
                "run_id": rows[0]["run_id"],
                "basin_lineage": rows[0]["basin_lineage"],
                "algorithm": rows[0]["algorithm"],
                "rows": len(rows),
                "positive_rows": labels.get("POSITIVE", 0),
            }
        ],
    )
    write_csv(
        args.output_dir / "split_manifest.csv",
        ["group", "split", "reason"],
        [
            {
                "group": rows[0]["basin_lineage"],
                "split": "UNASSIGNED",
                "reason": "Only one lineage; grouped train/validation split is not yet valid.",
            }
        ],
    )
    readiness = {
        "status": "NOT_READY",
        "rows": len(rows),
        "lineages": len({row["basin_lineage"] for row in rows}),
        "labels": dict(labels),
        "reasons": [
            "Only one targeted-repair lineage is available.",
            "Positive count is too small for stable LR or LightGBM evaluation.",
            "Grouped train/validation/test splitting is impossible without leakage.",
        ],
        "next_data_action": (
            "Continue logging candidate-value events across independent master/ALNS lineages; "
            "do not train ML yet."
        ),
    }
    write_json(args.output_dir / "dataset_diagnostics.json", readiness)
    (args.output_dir / "ML_READINESS.md").write_text(
        "# Q1 ML Readiness\n\n"
        "**NOT_READY.** The dataset contains "
        f"{len(rows)} evaluated candidate events from one lineage and "
        f"{labels.get('POSITIVE', 0)} positive event(s). A leakage-safe grouped split is "
        "not possible yet. Continue candidate-value logging; do not train LR/LightGBM in this stage.\n",
        encoding="utf-8",
    )
    print(
        f"Q1 ML FOUNDATION: NOT_READY rows={len(rows)} positives={labels.get('POSITIVE', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
