"""Quick probe: fast pricing on one real subproblem (root duals)."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

WORKTREE = Path(
    r"C:\Users\shiju\.codex\visualizations\2026\08\15\01a00527-e2b2-7dc3-993f-30ef424963b0\q1-exact-certification"
)
sys.path.insert(0, str(WORKTREE))

from src.solver import load_problem_data  # noqa: E402
from src.solver.q1_fast_pricing import fast_exact_pricing  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "cert", Path(r"d:\Desktop\B题\.sync_tmp\22_certify_fast_pricing.py")
)
cert = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cert)
cert.EXACT = WORKTREE / "outputs" / "q1" / "exact"
directory = (
    WORKTREE / "outputs" / "q1" / "exact" / "column-generation"
    / "20260816-fullspace-cg" / "iteration-015"
)

data = load_problem_data()
demand_duals, branch_duals = cert.load_duals(directory)
reference = json.loads(
    (directory / "pricing-A01-T1.json").read_text(encoding="utf-8")
)
print("MILP reference: rc=", reference["reduced_cost"],
      "elapsed=", reference["elapsed_seconds"])
started = time.perf_counter()
result, diag = fast_exact_pricing(
    data, demand_duals, "A01", "T1",
    branch_duals=branch_duals, return_diagnostics=True,
)
wall = time.perf_counter() - started
print(f"FAST: rc={result.reduced_cost} wall={wall:.3f}s")
print("diag:", diag)
print("stops:", [(s.facility_id, s.refuel) for s in result.stops])
print("pattern:", result.allocation_pattern)
