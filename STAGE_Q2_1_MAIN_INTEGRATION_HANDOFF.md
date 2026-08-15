# Q2-1 Main Integration Handoff

Date: 2026-08-15  
Branch: `codex/q2-main-integration`  
Base: `main@eabfcfe`  
Q2 source audited: `platinumist_update@c37ea9b` (`code/` copy)

## Outcome

Q2's restricted candidate-route master is integrated on top of main without
merging or replacing the shared solver core. The canonical 19,736-minute Q2
artifact loads, re-exports and independently validates exactly on main.

The fresh-solve gate did **not** pass in the current environment. A fresh
195/105-second run produced 20,275 minutes. Running the original `c37ea9b` code
with its original candidate pickle in the same Python/SciPy environment
produced exactly the same 20,275 solution. Candidate counts, content and order
also have the same hash. The regression is therefore a solver-environment/
search-trajectory difference, not Q2 integration semantic drift.

Per the approved gate, primary-heavy, primary-only and old/new Q1 seed A/B
experiments were not started. Q2-2 was not entered.

## Integrated interfaces

- `ProblemData.q2_pools` and `q2_passenger_count` load and validate the 264 Q2
  OD groups.
- `load_q2_solution` restores all 4,000 people, preserves service occurrences,
  re-evaluates routes and requires exact coverage.
- Q2 variants call one run-scoped `SolverCache`; technical-stop augmentation
  and per-leg physics remain main's implementations.
- `Q2MasterConfig` exposes independent primary and secondary time limits;
  secondary limit zero is a real primary-only mode with null secondary status.
- Run diagnostics record input hashes, candidate hash/counts, solver version,
  cache statistics, elapsed times and `bound_scope=restricted_master`.
- `scripts/05_solve_q2.py` writes immutable runs and atomically replaces
  `best`, preventing stale `q2-pair-*` files.

## Canonical baseline

Source:
`c37ea9b:code/outputs/q2/runs/20260815-q2-bounded-lex-pair`

| Metric | Value |
|---|---:|
| Aircraft time | **19,736 min** |
| Passenger time | 270,734 min |
| Flights | 107 |
| Fuel | 152,910.4 kg |
| Seat utilization | 0.8182982554 |
| Served | 4,000/4,000 |
| Validator | PASS, 0 issues |

The replay is stored in `outputs/q2/baseline-19736`, the same atomic contents
are in `outputs/q2/best`, and the originating immutable run is retained under
`outputs/q2/runs`.

## Fresh-solve audit

Environment: Python 3.11.9, SciPy 1.17.1,
`scipy.optimize.milp/HiGHS`.

| Field | Canonical record | Current fresh run |
|---|---:|---:|
| Candidate sequences | 357 | 357 |
| Candidate variants | 3,116 | 3,116 |
| Compatible assignments | 23,251 | 23,251 |
| Candidate pool hash | not previously recorded | `3ccc374360b9f227923b2fa68690b1291931276e050c19aece75a6c0eadbc882` |
| Aircraft-time incumbent | 19,736 | 20,275 |
| Restricted dual bound | 16,296 | 16,292 |
| Restricted gap | 17.430% | 19.645% |
| Validator | PASS | PASS |

The old tracked pickle produces the same new pool hash and was used only for
the audit comparison, never as the integrated solver's result source. The
integrated fresh run regenerated all variants through main's shared cache.

The original `c37ea9b` implementation, executed in the current environment,
returned the same metrics as the integrated run:

`20,275 / 261,763 / 109 / 157,594.0 / 0.7686391979`.

This is the decisive no-drift control.

## Tests and Q1 regression

- `pytest`: **47 passed**.
- Q1 Validator: PASS.
- Q1 load/re-export: route and assignment CSVs byte-identical.
- Q1 golden metrics unchanged:
  `15,371 / 120,870 / 95 / 123,081.7 / 0.4822550212`.
- Golden no-op relocation configuration
  (`targets=2`, `iterations=8`) accepted zero moves.

A wider pre-existing Q1 relocation configuration (`targets=4`, `iterations=30`)
found a 15,366-minute legal solution. It was not promoted or incorporated,
because Q1 optimisation is outside Q2-1 and the approved seed A/B explicitly
uses 15,371.

## Experiment disposition

The complete audit table is `outputs/q2/experiment-summary.csv`.

- canonical artifact replay: PASS;
- integrated fresh A0: legal but fresh gate failed;
- old-code/current-environment control: exactly matches integrated A0;
- A1 270/30: skipped by gate;
- A2 300/0: skipped by gate;
- old15,418/new15,371 seed A/B: skipped by gate;
- secondary polish: not applicable.

## Next decision

Do not start Q2-2 from this handoff automatically. The remaining Q2-1 choice
is to reproduce/pin the original SciPy/HiGHS environment or explicitly approve
the bounded MIP-start feasibility spike described in the plan. Until then,
19,736 remains the only promoted Q2 best and no current-environment fresh run
is represented as reproducing it.

