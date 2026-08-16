from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import sha256, write_csv, write_json


RUN_IDS = (
    "20260815-q2-round3-long-s401",
    "20260816-q2-round3-long-s402",
    "20260816-q2-round3-long-s403",
    "20260816-q2-round3-finalist-s404",
    "20260816-q2-round3-ml501",
    "20260816-q2-round3-ml502",
    "20260816-q2-round3-ml503",
    "20260816-q2-round3-final-geometry-s505",
    "20260816-q2-round3-ml601-independent",
    "20260816-q2-round3-ml601-cont",
    "20260816-q2-round3-ml602-independent",
)
LINEAGE_PATH = (
    "20260815-q2-round3-long-s401",
    "20260816-q2-round3-long-s402",
    "20260816-q2-round3-long-s403",
    "20260816-q2-round3-finalist-s404",
    "20260816-q2-round3-ml501",
    "20260816-q2-round3-final-geometry-s505",
)


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _trajectory(run_dir: Path) -> dict[str, object]:
    config = _json(run_dir / "run_config.json")
    metrics = _json(run_dir / "metrics.json")
    rows = [json.loads(line) for line in (run_dir / "search-log.jsonl").read_text(encoding="utf-8").splitlines()]
    cumulative = 0.0
    last_best_runtime = 0.0
    last_best_repair = 0
    first_primary_runtime = None
    initial_primary = int(metrics["initial_metrics"]["total_aircraft_time_minutes"])
    for index, row in enumerate(rows, 1):
        cumulative += float(row.get("runtime", 0.0))
        if row.get("new_best"):
            last_best_runtime = cumulative
            last_best_repair = index
        if first_primary_runtime is None and int(row["best_objective"]) < initial_primary:
            first_primary_runtime = cumulative
    return {
        "run_id": config["run_id"],
        "purpose": config["config"].get("run_purpose"),
        "lineage_id": config["config"].get("lineage_id"),
        "parent_run_id": config["config"].get("parent_run_id"),
        "initial_aircraft": initial_primary,
        "final_aircraft": int(metrics["validator_metrics"]["total_aircraft_time_minutes"]),
        "passenger_time": int(metrics["validator_metrics"]["total_passenger_travel_time_minutes"]),
        "flights": int(metrics["validator_metrics"]["total_flights"]),
        "fuel": float(metrics["validator_metrics"]["total_fuel_consumption_kg"]),
        "iterations": len(rows),
        "accepted": sum(int(bool(row.get("accepted"))) for row in rows),
        "new_best_count": sum(int(bool(row.get("new_best"))) for row in rows),
        "flight_eliminations": sum(
            int(bool(row.get("accepted") and row.get("route_ejected"))) for row in rows
        ),
        "runtime_seconds": float(config["total_elapsed_seconds"]),
        "runtime_to_best_seconds": round(last_best_runtime, 6),
        "repairs_to_best": last_best_repair,
        "time_to_first_primary_improvement_seconds": (
            round(first_primary_runtime, 6) if first_primary_runtime is not None else None
        ),
        "stop_reason": config["search_statistics"].get("stop_reason"),
        "terminal_stagnation": config["search_statistics"].get("terminal_stagnation"),
    }


def main() -> int:
    q2_root = ROOT / "outputs" / "q2"
    runs = [_trajectory(q2_root / "runs" / run_id) for run_id in RUN_IDS]
    run_by_id = {str(row["run_id"]): row for row in runs}
    diagnostics = _json(q2_root / "ml-data-round3" / "dataset_diagnostics.json")
    positive_source = Counter()
    novel_stop = Counter()
    positive_operator = Counter()
    exact_to_best = Counter()
    last_best_iteration: dict[str, int] = {}
    for run_id in LINEAGE_PATH:
        logs = [
            json.loads(line)
            for line in (q2_root / "runs" / run_id / "search-log.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        last_best_iteration[run_id] = max(
            (int(row["iteration"]) for row in logs if row.get("new_best")),
            default=-1,
        )
    with (q2_root / "ml-data-round3" / "candidate_events.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        for row in csv.DictReader(stream):
            run_id = row["run_id"]
            if (
                run_id in last_best_iteration
                and int(row["iteration"]) <= last_best_iteration[run_id]
                and row["label_class"] != "CENSORED"
            ):
                exact_to_best[run_id] += 1
            if row["label_class"] != "POSITIVE":
                continue
            source = row.get("candidate_source") or "OTHER"
            positive_source[source] += 1
            positive_operator[row.get("destroy_operator") or "OTHER"] += 1
            if row.get("is_incumbent_sequence") == "False":
                try:
                    length = len(json.loads(row["candidate_sequence"]))
                except (TypeError, json.JSONDecodeError):
                    length = int(float(row.get("feature_service_node_count") or 0))
                novel_stop[length] += 1

    final = _json(q2_root / "best" / "metrics.json")["validator_metrics"]
    controls = [
        ("canonical-rmp", 19736, 270734, 107, 152910.4, 0.8182982554006456),
        ("standard-alns", 18906, 266308, 103, 146442.5, 0.853703500006346),
        ("round1", 17958, 263588, 97, 138075.2, 0.8974631474136977),
        ("round2", 17595, 259487, 96, 135954.1, 0.910212354617489),
        (
            "round3-final",
            int(final["total_aircraft_time_minutes"]),
            int(final["total_passenger_travel_time_minutes"]),
            int(final["total_flights"]),
            float(final["total_fuel_consumption_kg"]),
            float(final["seat_utilization"]),
        ),
    ]
    final_rows = [
        {
            "solution": name,
            "aircraft_time": aircraft,
            "passenger_time": passenger,
            "flights": flights,
            "fuel": fuel,
            "utilization": utilization,
            "improvement_vs_round3_final": aircraft - int(final["total_aircraft_time_minutes"]),
        }
        for name, aircraft, passenger, flights, fuel, utilization in controls
    ]
    write_csv(
        ROOT / "Q2_ROUND3_FINAL_COMPARISON.csv",
        tuple(final_rows[0]),
        final_rows,
    )
    cumulative_runtime = sum(float(run_by_id[item]["runtime_seconds"]) for item in LINEAGE_PATH[:-1])
    cumulative_repairs = sum(int(run_by_id[item]["iterations"]) for item in LINEAGE_PATH[:-1])
    final_run = run_by_id[LINEAGE_PATH[-1]]
    summary = {
        "schema_version": 2,
        "round2_control": 17595,
        "final_metrics": final,
        "improvements": {
            str(name): {
                "minutes": aircraft - int(final["total_aircraft_time_minutes"]),
                "percent": round(100.0 * (aircraft - int(final["total_aircraft_time_minutes"])) / aircraft, 6),
            }
            for name, aircraft, *_ in controls[:-1]
        },
        "lowest_flights": int(final["total_flights"]),
        "best_95_metrics": final,
        "winning_run": "20260816-q2-round3-final-geometry-s505",
        "winning_primary_run": "20260816-q2-round3-ml501",
        "cumulative_lineage_runtime_to_final_best_seconds": round(
            cumulative_runtime + float(final_run["runtime_to_best_seconds"]), 6
        ),
        "cumulative_repairs_to_final_best": cumulative_repairs + int(final_run["repairs_to_best"]),
        "exact_evaluations_to_best_by_run": dict(exact_to_best),
        "runs": runs,
        "absorption": {
            "full_diagnostic_windows": 22,
            "formal_flushed_windows": 5,
            "found_95_in_forced_windows": False,
            "six_route_decision": "REJECT_COST_EXPLOSION",
            "promising_master_queue_entries": 0,
            "deep_resolve_count": 0,
            "actual_96_to_95_event": {
                "run_id": "20260816-q2-round3-long-s402",
                "iteration": 124,
                "operator": "low_utilization_route",
                "neighborhood": [15, 69, 14, 24],
                "local_master_size": 806,
                "primary_gain": 100,
                "flight_delta": 1,
                "restricted_gap": 0.0,
            },
        },
        "components": {
            "path_relink_v2": {"attempts": 4, "accepted": 0, "primary_gain": 0, "runtime_seconds": 145.281},
            "targeted_five_route_primary_gain": 1,
            "cross_exchange_round3_primary_gain": 0,
            "land_heavy_primary_gain_s401_s402": 60,
            "low_utilization_primary_gain_s401_s402": 223,
            "cross_airport_regrouping_direct_gain": 0,
        },
        "ml": {
            **diagnostics,
            "positive_by_candidate_source": dict(positive_source),
            "positive_by_operator": dict(positive_operator),
            "novel_positive_by_service_node_count": dict(novel_stop),
            "absorption_positives": positive_source["ABSORPTION"],
            "context_only_positives": positive_source["CONTEXT_ONLY"],
            "cross_exchange_positives": positive_source["CROSS_EXCHANGE"],
            "path_relink_positives": positive_source["PATH_RELINK"],
            "decision": "READY",
            "target": "P(MILP-selected AND accepted useful repair | exact-evaluated candidate, context)",
        },
    }
    write_json(q2_root / "round3-experiment-summary.json", summary)
    write_json(
        ROOT / "ROUND3_CONTROL_MANIFEST.json",
        {
            "round2_frozen_commit": "c99c1b8d0e9bcfe6900b89a2979cf745d855f302",
            "round3_base_commit": "c99c1b8d0e9bcfe6900b89a2979cf745d855f302",
            "round3_code_checkpoint": _git("rev-parse", "HEAD"),
            "branch": _git("branch", "--show-current"),
            "round2_control": {
                "routes_sha256": "2fdb2e117a0cbe977b0512832002c612c7dfb60e450375f045106d94fbf60c63",
                "assignments_sha256": "309cceb1d207b4d838f2885e4b6644dea5e00f19b5958aea1cf34075edcea551",
                "aircraft_time": 17595,
                "flights": 96,
            },
            "round3_final": {
                "routes_sha256": sha256(q2_root / "best" / "q2-routes.csv"),
                "assignments_sha256": sha256(q2_root / "best" / "q2-assignments.csv"),
                "metrics": final,
                "source_run": "20260816-q2-round3-final-repro",
            },
            "bound_scope": "restricted_local_master",
        },
    )
    write_json(
        q2_root / "repeated-visits-round3-assessment.json",
        {
            "decision": "REJECT",
            "evidence": "Validated 95-flight incumbent uses distinct service occurrences and no accepted repair required F_i -> ... -> F_i.",
            "implemented": False,
        },
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
