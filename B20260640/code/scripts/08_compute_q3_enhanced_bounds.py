from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_json
from src.solver import load_problem_data
from src.solver.q3 import load_q3_people, load_q3_variants, transport_time_lower_bound
from src.solver.q3_bounds import (
    candidate_route_master_lp_bound,
    layered_multicommodity_flow_bound,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="计算问题三增强理论下界")
    parser.add_argument(
        "--variant-cache", type=Path, default=ROOT / "outputs/q2/pair_n3_h10.pkl"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "outputs/q3/best/q3-enhanced-bounds.json"
    )
    parser.add_argument(
        "--bounds-summary", type=Path, default=ROOT / "outputs/q3/best/q3-bounds.json"
    )
    parser.add_argument(
        "--incumbent-metrics",
        type=Path,
        default=ROOT / "outputs/q3/best/metrics.json",
        help="用于计算相对间隙的现行解 metrics.json",
    )
    parser.add_argument("--time-limit", type=float, default=300.0)
    args = parser.parse_args()

    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    mandatory = [person for person in people.values() if person.mandatory]
    variants = load_q3_variants(args.variant_cache, people.values(), data.config)
    passenger_work = transport_time_lower_bound(mandatory, data)
    network = layered_multicommodity_flow_bound(
        mandatory, data, time_limit_seconds=args.time_limit
    )
    candidate = candidate_route_master_lp_bound(
        mandatory, variants, data, time_limit_seconds=args.time_limit
    )
    incumbent = json.loads(
        args.incumbent_metrics.read_text(encoding="utf-8")
    )["baseline_metrics"]["total_aircraft_time_minutes"]
    global_lower_bound = max(passenger_work, network.objective_minutes_integer_ceiling)
    payload = {
        "incumbent_aircraft_time_minutes": incumbent,
        "global_bounds": {
            "passenger_work_lower_bound_minutes": passenger_work,
            "layered_multicommodity_flow": network.to_dict(),
            "enhanced_global_lower_bound_minutes": global_lower_bound,
            "certified_gap_percent": round(
                100.0 * (incumbent - global_lower_bound) / incumbent, 6
            ),
        },
        "candidate_pool_reference": {
            **candidate.to_dict(),
            "reference_gap_percent": round(
                100.0
                * (incumbent - candidate.objective_minutes_integer_ceiling)
                / incumbent,
                6,
            ),
        },
        "interpretation": {
            "global": (
                "The enhanced global bound is valid for the original Q3 first stage "
                "because every original schedule maps to the continuous layered-flow LP."
            ),
            "candidate_pool": (
                "The candidate-route LP is only a finite-pool reference bound and must "
                "not be used as a global optimality certificate."
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, payload)
    if args.bounds_summary.exists():
        summary = json.loads(args.bounds_summary.read_text(encoding="utf-8"))
    else:
        summary = {}
    stage1 = summary.setdefault("stage1", {})
    stage1.update(
        {
            "incumbent_upper_bound_minutes": incumbent,
            "seat_km_transport_lower_bound_minutes": passenger_work,
            "layered_multicommodity_flow_lower_bound_minutes": network.objective_minutes_integer_ceiling,
            "enhanced_global_lower_bound_minutes": global_lower_bound,
            "conservative_gap_percent": round(
                100.0 * (incumbent - global_lower_bound) / incumbent, 6
            ),
            "incumbent_excess_over_lower_bound_percent": round(
                100.0 * (incumbent - global_lower_bound) / global_lower_bound, 6
            ),
            "finite_candidate_pool_lp_reference_minutes": candidate.objective_minutes_integer_ceiling,
            "finite_candidate_pool_reference_gap_percent": round(
                100.0
                * (incumbent - candidate.objective_minutes_integer_ceiling)
                / incumbent,
                6,
            ),
            "candidate_pool_reference_is_global_bound": False,
        }
    )
    args.bounds_summary.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.bounds_summary, summary)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
