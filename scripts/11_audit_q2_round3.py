from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_csv, write_json
from src.solver import (
    absorption_potential_ranking,
    audit_q2_solution,
    load_problem_data,
    load_q2_solution,
    q2_basin_fingerprint,
)


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    write_csv(path, tuple(rows[0]) if rows else (), rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the Q2 Round-3 incumbent")
    parser.add_argument(
        "--start-dir", type=Path, default=ROOT / "outputs" / "q2" / "best"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "outputs" / "q2" / "round3-audit-17595",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    data = load_problem_data()
    solution = load_q2_solution(
        args.start_dir / "q2-routes.csv",
        args.start_dir / "q2-assignments.csv",
        data,
        method="q2_round3_structural_audit",
    )
    route_rows, pair_rows = audit_q2_solution(solution, data)
    ranking = absorption_potential_ranking(route_rows, pair_rows)
    _write_rows(args.output_dir / "route-audit.csv", route_rows)
    _write_rows(args.output_dir / "route-pair-compatibility.csv", pair_rows)
    _write_rows(args.output_dir / "absorption-ranking.csv", ranking)
    summary = {
        "schema_version": 2,
        "metrics": solution.metrics.to_dict(),
        "route_count": len(solution.routes),
        "basin_fingerprint": q2_basin_fingerprint(solution),
        "top_absorption_routes": [
            {
                "route_index": row["route_index"],
                "rank": row["absorption_rank"],
                "score": row["absorption_score"],
                "aircraft_time": row["aircraft_time"],
                "passenger_count": row["passenger_count"],
                "utilization": row["utilization"],
                "land_fraction": row["land_fraction"],
                "base_airport": row["base_airport"],
                "service_sequence": row["service_sequence"],
            }
            for row in ranking[:12]
        ],
    }
    write_json(args.output_dir / "audit-summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

