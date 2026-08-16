"""Temporary: add debug print to _consider_termination in worktree file."""
import pathlib

p = pathlib.Path(
    r"C:\Users\shiju\.codex\visualizations\2026\08\15\01a00527-e2b2-7dc3-993f-30ef424963b0"
    r"\q1-exact-certification\src\solver\q1_fast_pricing.py"
)
s = p.read_text(encoding="utf-8")
anchor = "        incumbent_updates += 1\n        incumbent_rc = float(rc)"
assert anchor in s, "anchor missing"
inject = (
    anchor
    + "\n        import os"
    + "\n        if os.environ.get('FAST_PRICING_DEBUG'):"
    + "\n            print('INCUMBENT rc=', round(rc, 4),"
    + " 'cost=', round(cost, 4), 'fuel=', round(fuel, 2),"
    + " 'clock=', clock, 'path=', path, 'flags=', refuels)"
)
s = s.replace(anchor, inject, 1)
p.write_text(s, encoding="utf-8")
print("patched")
