"""Smoke test: Brute Force vs MILP vs Fast exact pricing on tiny universe."""
from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] if False else None

WORKTREE = r"C:\Users\shiju\.codex\visualizations\2026\08\15\01a00527-e2b2-7dc3-993f-30ef424963b0\q1-exact-certification"
sys.path.insert(0, WORKTREE)

from src.solver import load_problem_data  # noqa: E402
from src.solver.q1_pricing import ArcBranchRow, exact_pricing  # noqa: E402
from src.solver.q1_fast_pricing import fast_exact_pricing  # noqa: E402

# Import brute-force reference from the existing test module.
spec = importlib.util.spec_from_file_location(
    "tbp", Path(WORKTREE) / "tests" / "test_q1_branch_pricing.py"
)
tbp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tbp)


def run_case(name, duals, branch_duals, nodes, max_landings=3, base="A01",
             aircraft="T1", multiplier=1.0):
    data = tbp._tiny_data()
    brute = tbp._brute_force_branch_price(
        data, duals, base, aircraft, nodes, max_landings, branch_duals,
        route_cost_multiplier=multiplier,
    )
    milp = exact_pricing(
        data, duals, base, aircraft, candidate_nodes=nodes,
        max_landings=max_landings, branch_duals=branch_duals,
        route_cost_multiplier=multiplier,
    )
    fast = fast_exact_pricing(
        data, duals, base, aircraft, candidate_nodes=nodes,
        max_landings=max_landings, branch_duals=branch_duals,
        route_cost_multiplier=multiplier,
    )
    problems = []
    if abs(milp.reduced_cost - brute[0]) > 1e-6:
        problems.append(f"MILP rc {milp.reduced_cost} != brute {brute[0]}")
    if abs(fast.reduced_cost - brute[0]) > 1e-6:
        problems.append(f"FAST rc {fast.reduced_cost} != brute {brute[0]}")
    if fast.route_duration_minutes != brute[1]:
        problems.append(
            f"FAST duration {fast.route_duration_minutes} != brute {brute[1]}"
        )
    if tuple(fast.allocation_pattern) != tuple(brute[2]):
        problems.append(
            f"FAST pattern {fast.allocation_pattern} != brute {brute[2]}"
        )
    status = "OK" if not problems else "FAIL: " + "; ".join(problems)
    print(f"[{name}] rc={fast.reduced_cost:.6f} dur={fast.route_duration_minutes} {status}")
    return not problems


def main():
    nodes = ("F001", "F006", "F010")
    duals = {
        ("A01", "F001"): 13.25,
        ("LAND", "F006"): 9.5,
        ("A01", "F010"): 7.75,
        ("A02", "F001"): 1000.0,
    }
    configs = [
        ("plain", {}),
        ("le1", {ArcBranchRow(("A01", "F001"), "<=", 1): -7.0}),
        ("ge1", {ArcBranchRow(("A01", "F001"), ">=", 1): -7.0}),
        ("two", {
            ArcBranchRow(("A01", "F001"), "<=", 1): -3.5,
            ArcBranchRow(("F001", "F006"), ">=", 1): -5.25,
        }),
        ("three", {
            ArcBranchRow(("A01", "F001"), "<=", 1): -1.0,
            ArcBranchRow(("F001", "F006"), ">=", 1): -2.0,
            ArcBranchRow(("F006", "A01"), "<=", 1): -4.0,
        }),
        ("selfloop", {ArcBranchRow(("F001", "F001"), ">=", 1): -50.0}),
    ]
    ok = True
    for name, bd in configs:
        ok &= run_case(name, duals, bd, nodes)
    # Phase-I multiplier semantics.
    ok &= run_case("phase1", duals, configs[2][1], nodes, multiplier=0.0)
    # Randomized duals, 25 rounds.
    rng = random.Random(20260816)
    keys = list(duals)
    for trial in range(25):
        rand_duals = {key: rng.uniform(-20.0, 30.0) for key in keys}
        bd = {}
        if trial % 2 == 0:
            bd = {ArcBranchRow(rng.choice([
                ("A01", "F001"), ("F001", "F006"), ("F006", "A01"),
                ("F001", "F010"), ("A01", "F010"),
            ]), rng.choice(["<=", ">="]), rng.randint(1, 3)):
                -rng.uniform(0.0, 10.0)}
        ok &= run_case(
            f"rand{trial:02d}", rand_duals, bd, nodes,
            max_landings=rng.choice([2, 3, 4]),
        )
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
