# Q3 PRO Baseline Audit

Audit time: `2026-08-16T00:03:28+08:00`  
Repository: `codex/q3-pro@e0041235f5d11f36e0af1a6f4f680a8b5f6d6b57`

## Canonical truth from exported CSV and independent Validator

| Stage | Mandatory | Temporary | Aircraft min | Passenger min | Flights | Fuel kg | Seat utilization | Validator |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Stage 1 | 3840/3840 | 0 | 29659 | 241073 | 168 | 235329.4 | 0.5599499702 | valid, 0 issues |
| Stage 2 | 3840/3840 | 158/160 | 29659 | 250494 | 168 | 235329.4 | 0.5820689972 | valid, 0 issues |

- Strict Stage 2 cap: `29659` aircraft minutes, inherited from the current valid Stage 1 incumbent.
- Unserved temporary IDs: `P1102`, `P3290`.
- Revalidated files: `outputs/q3/best/q3-base-{routes,assignments}.csv` and `outputs/q3/best/q3-{routes,assignments}.csv`.
- The Stage 1 and Stage 2 route CSVs have the same SHA-256 because the current solutions share the same 168 flights; the assignment CSVs differ as expected.
- In-memory reports are not trusted for promotion unless they match these exported-CSV metrics.

## Trust classification

- Canonical and independently revalidated: `outputs/q3/best/` and byte-identical solution CSVs in `outputs/q3/closure_p2_best/` and `outputs/q3/runs/q3-p2-v9-feedback-final/`.
- Valid but superseded: `outputs/q3/p0_p1_best/` at 30180 minutes and 158/160 temporary.
- Mixed checkpoint: `outputs/q3/runs/q3-p2-v9-final/`; its Stage 2 canonical CSV matches the 29659 incumbent, while its top-level Stage 1 CSV is an earlier checkpoint. Use the final-feedback directory for paired final results.
- Invalid experiment: `outputs/q3/runs/20260815-q3-v5-mixed/` (recorded 8 issues); never promote.

## Stale artifacts found

- `tests/test_q3_outputs.py` still requires the superseded 30510-minute, 160/160 Stage 2 result.
- `outputs/q3/best/q3-bounds.json` still records the old 30546 Stage 1 incumbent and 160/160 Stage 2 claim.
- `outputs/q3/best/q3-enhanced-bounds.json` and `q3-optimization-summary.json` retain old incumbent metadata even though the global lower-bound calculation itself is separately documented.
- `outputs/q3/best/bounds.json`, `metrics.json`, `Q3_P2_RESULTS.md`, and the canonical CSV files agree on 29659 and 158/160.
- Historical run configs contain source-machine absolute paths and are provenance records, not directly portable configs.

## Reproduction and provenance

- Official from-scratch runner: `cd code && python scripts/06_solve_q3.py --run-id <id> --start-count 12 --deep-top-k 3 --enable-flexible-regret --multiflight-rr --run-p2`.
- Final promotion must remain an aggregator action after CSV export, independent validation, and metric equality checks.
- Route cache snapshot: `outputs/q2/pair_n3_h10.pkl`, 2,350,607 bytes, SHA-256 `04df605cfdd57e6f45c0bb62709c2f96808a23f511fa1a6dda2eac70aeec7a85`.
- Cache deserialization succeeds. Recorded route variant count: 3116. Q2 cache is read-only; Q3-generated routes will be stored only under `outputs/q3/q3-pro/route_library/`.

## Bounds and exact-status truth

- Globally valid passenger-work lower bound: 12389 minutes.
- Globally valid layered multicommodity-flow lower bound: 14125 minutes.
- Certified Stage 1 gap at UB 29659: 52.375333%.
- Finite candidate-pool LP reference: 15198 minutes; it is not a global lower bound.
- Stage 2 theoretical upper bound: 160 temporary; incumbent is 158.
- The 158 result is optimal only for the fixed final 168-flight assignment structure. No global 159/160 infeasibility certificate exists.

## Baseline test status

- Independent Validator: Stage 1 and Stage 2 both valid with zero issues.
- Q3-focused baseline suite: 18 passed, 1 failed.
- The sole failure is the stale canonical-output assertion in `tests/test_q3_outputs.py`; Validator semantics and all other Q3 regression tests pass.

This report is the sole baseline truth for subsequent Q3 PRO work. Every later incumbent must improve the applicable lexicographic key and pass the same export/Validator/metric-equality gate.
