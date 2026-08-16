from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import sha256, write_json
from src.solver import load_problem_data
from src.validation import validate_solution


def _key(metrics: dict[str, object]) -> tuple[float, ...]:
    return (
        float(metrics["total_aircraft_time_minutes"]),
        float(metrics["total_passenger_travel_time_minutes"]),
        float(metrics["total_flights"]),
        float(metrics["total_fuel_consumption_kg"]),
        -float(metrics["seat_utilization"]),
    )


def _method(directory: Path) -> str:
    text = str(directory).lower()
    if "final14730-control" in text:
        return "Master-recombined + Standard ALNS education"
    if "final14730-reheat" in text:
        return "Master-recombined + Standard ALNS education (reheat challenger)"
    if "relatedness-r3" in text:
        return "Master-recombined + Relatedness R3 education"
    if "targeted-neighborhoods" in text:
        return "88-flight Master + route cross-exchange"
    if "target-88" in text:
        return "Exact route-pool Master (88-flight constrained)"
    if "exact-master" in text or "strict" in text:
        return "Exact elite route-pool Master"
    return directory.name


def main() -> int:
    parser = argparse.ArgumentParser(description="Select and promote the final validated Q1 OR candidate")
    parser.add_argument(
        "--search-root",
        type=Path,
        default=ROOT / "outputs" / "q1" / "final-or",
    )
    args = parser.parse_args()
    candidates = []
    for validator_path in args.search_root.rglob("validator.json"):
        directory = validator_path.parent
        routes = directory / "q1-routes.csv"
        assignments = directory / "q1-assignments.csv"
        if not routes.exists() or not assignments.exists():
            continue
        payload = json.loads(validator_path.read_text(encoding="utf-8"))
        if payload.get("valid") and payload.get("metrics"):
            candidates.append((directory, payload["metrics"]))
    if not candidates:
        raise RuntimeError("No validated Q1 final-OR candidates found")
    candidates.sort(key=lambda item: (_key(item[1]), str(item[0])))
    winner_dir, stored_metrics = candidates[0]
    data = load_problem_data()
    validation = validate_solution(
        "q1",
        winner_dir / "q1-routes.csv",
        winner_dir / "q1-assignments.csv",
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    if not validation.valid or validation.metrics is None:
        raise RuntimeError("Selected winner failed fresh independent Validator")
    metrics = validation.metrics.to_dict()
    if _key(metrics) != _key(stored_metrics):
        raise RuntimeError("Selected winner metrics changed under fresh Validator")
    final_dir = ROOT / "outputs" / "q1" / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "q1-routes.csv",
        "q1-assignments.csv",
        "q1-convergence.csv",
        "operator_stats.csv",
    ):
        source = winner_dir / name
        target = final_dir / name
        if source.exists():
            shutil.copy2(source, target)
        elif target.exists() and name not in {"q1-routes.csv", "q1-assignments.csv"}:
            target.unlink()
    write_json(final_dir / "validator.json", validation.to_dict())
    write_json(
        final_dir / "metrics.json",
        {
            "gate_pass": True,
            "metrics_match": True,
            "validator_metrics": metrics,
            "comparison_key": list(_key(metrics)),
        },
    )
    method = _method(winner_dir)
    write_json(
        final_dir / "winning_config.json",
        {
            "method": method,
            "source_directory": str(winner_dir.resolve()),
            "selection_rule": "strict lexicographic among all independently validated final-OR candidates",
        },
    )
    write_json(
        final_dir / "method_metadata.json",
        {
            "stage": "Q1 Final OR / Matheuristic Intensification",
            "method": method,
            "lineage": "elite route pool -> exact master -> Standard/Relatedness education -> pool",
            "source_directory": str(winner_dir.resolve()),
            "routes_sha256": sha256(final_dir / "q1-routes.csv"),
            "assignments_sha256": sha256(final_dir / "q1-assignments.csv"),
            "validator": "VALID",
            "issues": 0,
        },
    )
    print(
        "Q1 FINAL OR PROMOTED: "
        f"method={method}, time={metrics['total_aircraft_time_minutes']}, "
        f"passenger={metrics['total_passenger_travel_time_minutes']}, "
        f"flights={metrics['total_flights']}, source={winner_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
