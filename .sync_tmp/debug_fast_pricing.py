"""Debug: inspect fast pricing winner vs brute force on the plain tiny case."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ["FAST_PRICING_DEBUG"] = "1"

WORKTREE = r"C:\Users\shiju\.codex\visualizations\2026\08\15\01a00527-e2b2-7dc3-993f-30ef424963b0\q1-exact-certification"
sys.path.insert(0, WORKTREE)

from src.solver import q1_fast_pricing as qfp  # noqa: E402
from src.solver.q1_fast_pricing import (  # noqa: E402
    _greedy_allocation,
    _signature_reward,
    fast_exact_pricing,
)

qfp._CROSS_CHECK_TOL = 1.0e9  # inspect the winner even if accounting differs

spec = importlib.util.spec_from_file_location(
    "tbp", Path(WORKTREE) / "tests" / "test_q1_branch_pricing.py"
)
tbp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tbp)

data = tbp._tiny_data()
nodes = ("F001", "F006", "F010")
duals = {
    ("A01", "F001"): 13.25,
    ("LAND", "F006"): 9.5,
    ("A01", "F010"): 7.75,
    ("A02", "F001"): 1000.0,
}
brute = tbp._brute_force_branch_price(data, duals, "A01", "T1", nodes, 3, {})
print("brute rc/duration/pattern:", brute)

aircraft = data.config.aircraft_types["T1"]
print("seats:", aircraft.seats, "tank:", aircraft.tank_capacity_kg,
      "reserve:", aircraft.reserve_kg, "speed:", aircraft.speed_kmh)
for key in [("A01", "F001"), ("A01", "F006"), ("A01", "F010"),
            ("F001", "F006"), ("F006", "F010"), ("F001", "F010"),
            ("F006", "A01"), ("F010", "A01"), ("F001", "A01")]:
    try:
        print(key, data.matrix[key[0]][key[1]])
    except KeyError:
        print(key, "MISSING")

try:
    result, diag = fast_exact_pricing(
        data, duals, "A01", "T1", candidate_nodes=nodes, max_landings=3,
        return_diagnostics=True,
    )
except RuntimeError as exc:
    print("RUNTIME ERROR:", exc)
    raise SystemExit(1)
print("fast rc:", result.reduced_cost, "duration:", result.route_duration_minutes)
print("fast pattern:", result.allocation_pattern)
print("fast stops:", [(s.facility_id, s.refuel) for s in result.stops])
print("diag:", diag)

visited = frozenset(stop.facility_id for stop in result.stops)
pattern = _greedy_allocation(data, duals, "A01", aircraft.seats, visited)
print("greedy on visited:", pattern)
