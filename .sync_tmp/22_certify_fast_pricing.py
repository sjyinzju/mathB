"""Gate 8.2 + Gate 9 certification of the fast exact pricing oracle.

Cross-checks ``fast_exact_pricing`` against the archived MILP reference
oracle results (``pricing-{base}-{type}.json``) on the real instance duals
of four B&P tree points: root (final CG iteration), N1-L, N1-R and N2-LL.
Every one of the nine base/type subproblems per dual source is compared on
minimum reduced cost, certification flags and elapsed time.

The MILP oracle remains the permanent correctness reference; this script
only reads its archived, previously validated outputs.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.solver import load_problem_data  # noqa: E402
from src.solver.q1_pricing import PRICING_TOL, ArcBranchRow  # noqa: E402
from src.solver.q1_fast_pricing import fast_exact_pricing  # noqa: E402

EXACT = ROOT / "outputs" / "q1" / "exact"
RC_TOL = 1.0e-6

SOURCES = [
    (
        "root-iter015",
        EXACT / "column-generation" / "20260816-fullspace-cg" / "iteration-015",
    ),
    (
        "N1-L-iter001",
        EXACT / "branch-and-price" / "20260816-root-children" / "N1-L"
        / "iteration-001",
    ),
    (
        "N1-R-iter002",
        EXACT / "branch-and-price" / "20260816-root-children" / "N1-R"
        / "iteration-002",
    ),
    (
        "N2-LL-iter001",
        EXACT / "branch-and-price" / "20260816-recursive-best-bound" / "N2-LL"
        / "iteration-001",
    ),
]


def load_duals(directory: Path):
    payload = json.loads((directory / "rmp.json").read_text(encoding="utf-8"))
    demand_duals = {}
    for key, value in payload["demand_duals"].items():
        origin, destination = key.split("->")
        demand_duals[(origin, destination)] = float(value)
    branch_duals = {}
    for item in payload.get("branch_duals", []):
        row = item["row"]
        branch_duals[ArcBranchRow(
            (row["arc"][0], row["arc"][1]), row["sense"], int(row["rhs"])
        )] = float(item["canonical_dual"])
    return demand_duals, branch_duals


def main() -> int:
    data = load_problem_data()
    records = []
    failures = 0
    for name, directory in SOURCES:
        demand_duals, branch_duals = load_duals(directory)
        print(f"=== {name}: {len(demand_duals)} duals, "
              f"{len(branch_duals)} branch rows ===")
        for base in data.config.airports:
            for aircraft_type in data.config.aircraft_types:
                reference_path = (
                    directory / f"pricing-{base}-{aircraft_type}.json"
                )
                reference = json.loads(
                    reference_path.read_text(encoding="utf-8")
                )
                started = time.perf_counter()
                fast, diag = fast_exact_pricing(
                    data, demand_duals, base, aircraft_type,
                    branch_duals=branch_duals,
                    route_cost_multiplier=float(
                        reference.get("route_cost_multiplier", 1.0)
                    ),
                    return_diagnostics=True,
                )
                wall = time.perf_counter() - started
                problems = []
                ref_rc = reference["reduced_cost"]
                if ref_rc is None:
                    if fast.reduced_cost is not None:
                        problems.append(
                            "MILP found no column but fast found "
                            f"rc={fast.reduced_cost}"
                        )
                else:
                    if fast.reduced_cost is None:
                        problems.append(
                            f"fast found no column, MILP rc={ref_rc}"
                        )
                    else:
                        delta = abs(fast.reduced_cost - float(ref_rc))
                        if delta > RC_TOL:
                            problems.append(
                                f"rc mismatch: fast={fast.reduced_cost} "
                                f"milp={ref_rc} delta={delta:.3e}"
                            )
                        if bool(reference["certified_no_negative_column"]) != bool(
                            fast.certified_no_negative_column
                        ):
                            problems.append("certification flag mismatch")
                        if reference["certified_no_negative_column"] and (
                            fast.reduced_cost < -PRICING_TOL
                        ):
                            problems.append(
                                "fast found negative column where MILP "
                                "certified none"
                            )
                speedup = (
                    float(reference["elapsed_seconds"]) / wall if wall else 0.0
                )
                status = "OK" if not problems else "FAIL"
                if problems:
                    failures += 1
                print(
                    f"[{name} {base}-{aircraft_type}] {status} "
                    f"fast_rc={fast.reduced_cost} milp_rc={ref_rc} "
                    f"fast={wall:.3f}s milp={reference['elapsed_seconds']}s "
                    f"speedup={speedup:.1f}x "
                    f"labels={diag.labels_created} kept={diag.labels_kept} "
                    f"domprune={diag.dominance_prunes} "
                    f"boundprune={diag.bound_prunes}"
                )
                for problem in problems:
                    print(f"    -> {problem}")
                records.append({
                    "source": name,
                    "base": base,
                    "aircraft_type": aircraft_type,
                    "fast_reduced_cost": fast.reduced_cost,
                    "milp_reduced_cost": ref_rc,
                    "fast_seconds": round(wall, 6),
                    "milp_seconds": reference["elapsed_seconds"],
                    "speedup": round(speedup, 2),
                    "diagnostics": {
                        "labels_created": diag.labels_created,
                        "labels_kept": diag.labels_kept,
                        "dominance_prunes": diag.dominance_prunes,
                        "bound_prunes": diag.bound_prunes,
                        "terminated_routes": diag.terminated_routes,
                        "incumbent_updates": diag.incumbent_updates,
                    },
                    "status": status,
                    "problems": problems,
                })
    out_dir = (
        EXACT / "fast-pricing-certification" / "20260816-gate82-real-duals"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "gate": "8.2 real-node MILP cross-check + 9 performance",
        "sources": [name for name, _ in SOURCES],
        "rc_tolerance": RC_TOL,
        "failures": failures,
        "records": records,
        "total_fast_seconds": round(
            sum(r["fast_seconds"] for r in records), 3
        ),
        "total_milp_seconds": round(
            sum(float(r["milp_seconds"]) for r in records), 3
        ),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8"
    )
    print(
        f"\nTOTAL fast={summary['total_fast_seconds']}s "
        f"milp={summary['total_milp_seconds']}s failures={failures}"
    )
    print(f"summary -> {out_dir / 'summary.json'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
