from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _run_command(
    *,
    run_id: str,
    start_dir: Path,
    seed: int,
    budget: str,
    policy: str,
    model: Path | None,
    geometry_slots: int,
    exploration_slots: int,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts/06_optimize_q2_lns.py"),
        "--run-id",
        run_id,
        "--start-dir",
        str(start_dir),
        "--iterations",
        "200",
        "--stagnation-patience",
        "50",
        "--checkpoint-interval",
        "500",
        "--neighborhood-size",
        "4",
        "--source-pool-size",
        "24",
        "--target-pool-size",
        "8",
        "--max-sequence-length",
        "5",
        "--candidate-sequence-budget",
        "24",
        "--local-primary-time-limit",
        "8",
        "--local-secondary-time-limit",
        "0",
        "--seed",
        str(seed),
        "--candidate-policy",
        policy,
        "--operators",
        "high_cost_route,low_utilization_route,shared_facility_flow,land_heavy_route",
        "--operator-selection",
        "adaptive_roulette",
        "--adaptive-reaction",
        "0.2",
        "--acceptance-policy",
        "sa",
        "--sa-initial-temperature",
        "12",
        "--sa-cooling-rate",
        "0.92",
        "--sa-min-temperature",
        "0.5",
        "--targeted-four-stop",
        "--ml-geometry-safeguard-slots",
        str(geometry_slots),
        "--exploration-slots",
        str(exploration_slots),
        "--censored-log-limit",
        "0",
        "--run-purpose",
        "optimization",
        "--lineage-id",
        run_id,
        "--restart-type",
        f"final_ml_ab_{budget}",
    ]
    if budget == "exact":
        command.extend(["--max-exact-evaluated-candidates", "2500"])
    elif budget == "wall":
        command.extend(["--wall-clock-limit", "45"])
    else:
        raise ValueError(budget)
    if model is not None:
        command.extend(["--ml-model", str(model)])
    return command


def _summarize_run(run_dir: Path, *, config_name: str, basin: str, budget: str) -> dict[str, object]:
    metrics = _read_json(run_dir / "metrics.json")
    run_config = _read_json(run_dir / "run_config.json")
    search = _read_jsonl(run_dir / "search-log.jsonl")
    candidates = _read_jsonl(run_dir / "candidate-log.jsonl")
    initial = metrics["initial_metrics"]
    final = metrics["validator_metrics"]
    statistics_row = run_config["search_statistics"]
    exact_by_iteration: dict[int, int] = {}
    positives = 0
    for row in candidates:
        if row.get("evaluation_state") == "exact_evaluated":
            iteration = int(row["iteration"])
            exact_by_iteration[iteration] = exact_by_iteration.get(iteration, 0) + 1
        positives += int(row.get("label_class") == "POSITIVE")
    target_iteration = next(
        (
            int(row["iteration"])
            for row in search
            if float(row["best_objective"]) <= 17218
        ),
        None,
    )
    initial_at_target = float(initial["total_aircraft_time_minutes"]) <= 17218
    time_to_target = 0.0 if initial_at_target else None
    exact_to_target = 0 if initial_at_target else None
    if target_iteration is not None and not initial_at_target:
        target_row = next(row for row in search if int(row["iteration"]) == target_iteration)
        time_to_target = sum(
            float(row.get("runtime", 0.0))
            for row in search
            if int(row["iteration"]) <= target_iteration
        )
        exact_to_target = sum(
            value for iteration, value in exact_by_iteration.items() if iteration <= target_iteration
        )
    exact_rows = int(statistics_row["exact_evaluated_candidate_rows"])
    accepted = int(statistics_row["accepted_moves"])
    useful_repairs = sum(
        int(
            bool(row.get("accepted"))
            and (
                float(row.get("primary_gain", 0)) > 0
                or float(row.get("secondary_gain", 0)) > 0
            )
        )
        for row in search
    )
    return {
        "run_id": run_config["run_id"],
        "budget_policy": budget,
        "config": config_name,
        "basin": basin,
        "seed": run_config["config"]["seed"],
        "initial_aircraft_time": initial["total_aircraft_time_minutes"],
        "initial_flights": initial["total_flights"],
        "best_aircraft_time": final["total_aircraft_time_minutes"],
        "passenger_time": final["total_passenger_travel_time_minutes"],
        "flights": final["total_flights"],
        "fuel": final["total_fuel_consumption_kg"],
        "utilization": final["seat_utilization"],
        "validator_pass": metrics["gate_pass"],
        "runtime_seconds": run_config["total_elapsed_seconds"],
        "lns_runtime_seconds": run_config["lns_elapsed_seconds"],
        "repairs": statistics_row["iterations_completed"],
        "exact_evaluated": exact_rows,
        "positive_candidates": positives,
        "positive_candidate_rate": positives / max(1, exact_rows),
        "accepted_moves": accepted,
        "accepted_useful_repairs": useful_repairs,
        "accepted_useful_repair_rate": useful_repairs
        / max(1, int(statistics_row["iterations_completed"])),
        "new_best_count": sum(int(bool(row.get("new_best"))) for row in search),
        "time_to_17218": time_to_target,
        "exact_evals_to_17218": exact_to_target,
        "ml_inference_seconds": statistics_row.get("ml_inference_seconds", 0.0),
        "ml_overhead_fraction": float(statistics_row.get("ml_inference_seconds", 0.0))
        / max(1.0e-9, float(run_config["lns_elapsed_seconds"])),
        "stop_reason": statistics_row["stop_reason"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fair Q2 geometry/ML online A/B")
    parser.add_argument("--budgets", default="exact,wall")
    parser.add_argument("--seeds", default="711,712")
    args = parser.parse_args()
    output_dir = ROOT / "outputs/q2/final-ml"
    output_dir.mkdir(parents=True, exist_ok=True)
    runs_root = ROOT / "outputs/q2/runs"
    starts = {
        "global_best": ROOT / "outputs/q2/best",
        "alternate_95": ROOT / "outputs/q2/runs/20260816-q2-round3-long-s402",
        "independent_ml602": output_dir / "warm-starts/ml602-independent",
    }
    offline = output_dir / "offline"
    configs = {
        "classical_geometry": ("geometry", None, 0, 0),
        "geometry_lr": ("hybrid_lr", offline / "q2_lr_ranker.joblib", 2, 1),
        "geometry_lightgbm": (
            "hybrid_lightgbm",
            offline / "q2_lightgbm_classifier_ranker.joblib",
            2,
            0,
        ),
        "hybrid_lightgbm_safe": (
            "hybrid_lightgbm",
            offline / "q2_lightgbm_classifier_ranker.joblib",
            2,
            1,
        ),
    }
    budgets = tuple(value.strip() for value in args.budgets.split(",") if value.strip())
    seeds = tuple(int(value) for value in args.seeds.split(",") if value.strip())
    rows: list[dict[str, object]] = []
    for budget in budgets:
        for basin, start_dir in starts.items():
            for seed in seeds:
                for config_name, (policy, model, geometry_slots, exploration_slots) in configs.items():
                    run_id = f"20260816-q2-final-ml-ab-{budget}-{basin}-{config_name}-s{seed}"
                    run_dir = runs_root / run_id
                    if not (run_dir / "metrics.json").exists():
                        completed = subprocess.run(
                            _run_command(
                                run_id=run_id,
                                start_dir=start_dir,
                                seed=seed,
                                budget=budget,
                                policy=policy,
                                model=model,
                                geometry_slots=geometry_slots,
                                exploration_slots=exploration_slots,
                            ),
                            cwd=ROOT,
                            check=False,
                            text=True,
                        )
                        if completed.returncode != 0:
                            raise RuntimeError(f"A/B run failed: {run_id}")
                    rows.append(
                        _summarize_run(
                            run_dir, config_name=config_name, basin=basin, budget=budget
                        )
                    )
                    _write_csv(output_dir / "Q2_ML_ONLINE_AB.csv", rows)

    summary_rows: list[dict[str, object]] = []
    for (budget, config_name), group in __import__("itertools").groupby(
        sorted(rows, key=lambda row: (row["budget_policy"], row["config"])),
        key=lambda row: (row["budget_policy"], row["config"]),
    ):
        values = list(group)
        objectives = [float(row["best_aircraft_time"]) for row in values]
        summary_rows.append(
            {
                "budget_policy": budget,
                "config": config_name,
                "runs": len(values),
                "best_aircraft_time": min(objectives),
                "median_aircraft_time": statistics.median(objectives),
                "mean_aircraft_time": statistics.mean(objectives),
                "worst_aircraft_time": max(objectives),
                "std_aircraft_time": statistics.pstdev(objectives),
                "median_exact_evaluated": statistics.median(
                    float(row["exact_evaluated"]) for row in values
                ),
                "median_runtime_seconds": statistics.median(
                    float(row["runtime_seconds"]) for row in values
                ),
                "mean_positive_candidate_rate": statistics.mean(
                    float(row["positive_candidate_rate"]) for row in values
                ),
                "mean_accepted_useful_repair_rate": statistics.mean(
                    float(row["accepted_useful_repair_rate"]) for row in values
                ),
                "mean_ml_inference_seconds": statistics.mean(
                    float(row["ml_inference_seconds"]) for row in values
                ),
                "runs_better_than_17218": sum(
                    float(row["best_aircraft_time"]) < 17218 for row in values
                ),
            }
        )
    _write_csv(output_dir / "online_ab_summary.csv", summary_rows)
    print(json.dumps(summary_rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
