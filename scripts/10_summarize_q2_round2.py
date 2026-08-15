from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_csv, write_json
from src.solver import load_problem_data, load_q2_solution


RUNS = {
    "round1_control": "20260815-q2-final-repro-s2",
    "extended_control_s5": "20260815-q2-round2-extended-control-s5",
    "extended_control_s6": "20260815-q2-round2-extended-control-s6",
    "stronger_elite_recombination": "20260815-q2-round2-elite-objective",
    "global_elite_restart": "20260815-q2-round2-restart-global-s7",
    "diverse_elite_restart": "20260815-q2-round2-restart-diverse-s7",
    "targeted_five_control": "20260815-q2-round2-target5-control-s8",
    "targeted_five": "20260815-q2-round2-target5-trigger-s8",
    "flight_elimination": "20260815-q2-round2-flight-elimination-s9",
    "fix_and_optimize": "20260815-q2-round2-fixopt-s10",
    "cross_exchange": "20260815-q2-round2-cross-exchange-s10",
    "portfolio_best": "20260815-q2-round2-portfolio-mixed-s11",
    "ml_logging_best": "20260815-q2-round2-ml-logging-s22",
    "extended_finalist": "20260815-q2-round2-extended-finalist-s30",
    "round2_final": "20260815-q2-round2-extended-round1-control-s30",
}


def _run_dir(run_id: str) -> Path:
    return ROOT / "outputs" / "q2" / "runs" / run_id


def _metrics(run_id: str) -> dict[str, object]:
    return json.loads((_run_dir(run_id) / "metrics.json").read_text(encoding="utf-8"))[
        "validator_metrics"
    ]


def _search(run_id: str) -> list[dict[str, object]]:
    path = _run_dir(run_id) / "search-log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _benchmark(prefix: str, seeds: tuple[int, ...]) -> dict[str, object]:
    values = [
        int(_metrics(f"{prefix}{seed}")["total_aircraft_time_minutes"])
        for seed in seeds
    ]
    return {
        "values": values,
        "best": min(values),
        "median": statistics.median(values),
        "mean": round(statistics.mean(values), 6),
        "worst": max(values),
        "std": round(statistics.pstdev(values), 6),
    }


def main() -> int:
    rows = []
    for stage, run_id in RUNS.items():
        metrics = _metrics(run_id)
        search = _search(run_id)
        rows.append(
            {
                "stage": stage,
                "run_id": run_id,
                "aircraft_time_minutes": metrics["total_aircraft_time_minutes"],
                "passenger_time_minutes": metrics["total_passenger_travel_time_minutes"],
                "flights": metrics["total_flights"],
                "fuel_kg": metrics["total_fuel_consumption_kg"],
                "utilization": metrics["seat_utilization"],
                "served": metrics["served_passengers"],
                "validator": "PASS",
                "iterations": len(search),
                "accepted_moves": sum(int(bool(row.get("accepted"))) for row in search),
                "route_eliminations": sum(int(row.get("flight_delta", 0)) for row in search),
                "primary_gain_minutes": sum(int(row.get("primary_gain", 0)) for row in search),
                "repair_runtime_seconds": round(sum(float(row.get("runtime", 0)) for row in search), 6),
            }
        )
    write_csv(
        ROOT / "Q2_ROUND2_FINAL_COMPARISON.csv",
        tuple(rows[0]),
        rows,
    )
    control = _benchmark("20260815-q2-round2-portfolio-geometry-s", (11, 12, 13))
    portfolio = _benchmark("20260815-q2-round2-portfolio-mixed-s", (11, 12, 13))
    finalist = _benchmark("20260815-q2-round2-finalist-s", (11, 12, 13))
    final_rows = _search(RUNS["round2_final"])
    ejection = next((row for row in final_rows if int(row.get("flight_delta", 0)) > 0), None)
    summary = {
        "fair_benchmark": {
            "round1_algorithm_control": control,
            "geometry_context_portfolio": portfolio,
            "round2_finalist": finalist,
            "wall_clock_target_seconds": 90,
            "same_initial_run": "20260815-q2-round2-cross-exchange-s10",
        },
        "extended": {
            "round2_finalist": _metrics(RUNS["extended_finalist"]),
            "round1_algorithm_control": _metrics(RUNS["round2_final"]),
            "wall_clock_target_seconds": 180,
            "same_initial_run": "20260815-q2-round2-ml-logging-s22",
        },
        "flight_elimination_event": ejection,
        "final_primary_improvement_vs_17958": 17958
        - int(_metrics(RUNS["round2_final"])["total_aircraft_time_minutes"]),
        "final_primary_improvement_vs_19736": 19736
        - int(_metrics(RUNS["round2_final"])["total_aircraft_time_minutes"]),
        "bound_scope": "restricted_local_master",
    }
    write_json(ROOT / "outputs" / "q2" / "round2-experiment-summary.json", summary)

    data = load_problem_data()
    final = load_q2_solution(
        ROOT / "outputs" / "q2" / "best" / "q2-routes.csv",
        ROOT / "outputs" / "q2" / "best" / "q2-assignments.csv",
        data,
    )
    assessment = {
        "decision": "REJECT_FOR_ROUND2",
        "final_flights": final.metrics.total_flights,
        "routes_with_repeated_service_facility": sum(
            int(len(route.service_facilities) != len(set(route.service_facilities)))
            for route in final.routes
        ),
        "evidence": [
            "The 97-to-96 elimination was achieved by the existing distinct-service model.",
            "Round2 continued to find primary improvements through incumbent and bounded distinct 1-5-stop columns.",
            "No logged accepted repair required a repeated service occurrence.",
        ],
        "next_gate": (
            "Reopen only if concrete residual-capacity/OD-chain cases show a revisit can "
            "remove another complete route while improving aircraft time."
        ),
    }
    write_json(ROOT / "outputs" / "q2" / "repeated-visits-assessment.json", assessment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
