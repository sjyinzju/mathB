# Q3 PRO Final Report

## Repository

- Source `origin/platinumist_update`: `e0041235f5d11f36e0af1a6f4f680a8b5f6d6b57`
- Q3 PRO run commit: `643a4c784c277ab74122033eeb70b74177d4045e`
- Run: `q3-pro-deep-v1`

## Final Stage 1

| Mandatory | Aircraft min | Passenger min | Flights | Fuel kg | Utilization | Validator |
|---:|---:|---:|---:|---:|---:|---|
| 3840/3840 | 29155 | 241018 | 165 | 231384.5 | 0.570217 | 0 issues |

## Final Stage 2

| Cap | Mandatory | Temporary | Aircraft min | Passenger min | Flights | Fuel kg | Utilization | Validator |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 29155 | 3840/3840 | 157/160 | 29155 | 250735 | 165 | 231384.5 | 0.592928 | 0 issues |

Unserved temporary IDs: `P1102, P2239, P3290`.

## Improvement History

- Trusted baseline Stage 1: 29659 min.
- Q3 PRO final Stage 1: 29155 min.
- Net Q3 PRO improvement: 504 min.
- Long ALNS iterations: 500; global improvements: 5; restarts: 7.
- Exact/fix-and-optimize windows: 12.
- Path-relink attempts: 84.

## Search Statistics and Ablation

| Module | Budget / attempts | Result | Marginal value |
|---|---:|---|---:|
| Neighborhood preprocessing | 1,210,722 feasible person-route-day entries | 3116 safe routes retained; 0 unsafe dominance deletions | runtime 10.40 s |
| Long-horizon ALNS | 500 iterations, 7 restarts | 5 global best discoveries | -504 aircraft min |
| Cross-day LNS | 77 attempts, 10 accepted | 2 global best discoveries | principal structural contributor |
| Random-related ruin/recreate | 56 attempts, 2 accepted | 1 global best discovery | part of -231 accepted min |
| Route polish | 63 attempts, 5 accepted | 2 global best discoveries | -190 accepted min |
| Other structural operators | 274 attempts | feasible but no accepted improvement | 0 min |
| Elite pool | 9 feasibility-checked in-memory states; 3 diverse anchors persisted with independent Validator reports | restart memory retained, mean distance 0.044619 | enabled |
| Bidirectional path relinking | 84 repaired intermediates | no improvement | 0 min |
| Exact LNS / fix-and-optimize | 12 windows | no improvement | 0 min |
| Stage 2 optional rescue | 3 levels + 4 targeted operators | fixed 157; no structural rescue | 0 temporary |
| RMP + batch pricing | 2681 columns added | LP 16613.127193 → 15197.677632 | -1415.449561 restricted-LP min |
| Global flow bound | 191124-variable LP | 14125 min, optimal | runtime 129.02 s |

Total run wall time: 2913.45 s. The ALNS failure histogram recorded 358
`no_accepted_repair` outcomes and no solver crash. Improvement-per-CPU-minute
fell to zero after repeated restarts; exact LNS and relinking independently
confirmed no further move in their tested neighborhoods.

## Final Feedback and Bottlenecks

Final Stage 2 → mandatory projection did not improve Stage 1, so feedback stopped
after one round as required. At cap 29155, the fixed-flight MILP is optimal at
157/160. All three unserved people are blocked by seat capacity on otherwise
time-compatible incumbent flights:

- `P1102`: F009 → F003;
- `P2239`: LAND → F035;
- `P3290`: F009 → F003.

This is a fixed-structure certificate only. Unrestricted 158/159/160 feasibility
under cap 29155 remains open.

## Robustness Summary

- Turnaround 30→40 creates 25 incumbent chain violations; 30→45 creates 27.
- Removing any of the three busiest aircraft makes the fixed-route recovery
  infeasible; this does not rule out recovery with new routes.
- Full details and examples are in `robustness.json`.


## Route Library

- Deduplicated routes: 3116.
- Routes used by final Stage 1: 143.
- Q2 cache remained read-only; SHA-256 `04df605cfdd57e6f45c0bb62709c2f96808a23f511fa1a6dda2eac70aeec7a85`.

## Bounds and exact status

- Globally valid lower bound: 14125 min.
- Best feasible UB: 29155 min.
- Certified gap: 51.552049%.
- Restricted-master LP: 16613.127193 min.
- Full finite-pool LP after batch pricing: 15197.677632 min.
- The finite-pool LP is not a global bound.
- Stage 2 fixed-flight assignment status: `Optimization terminated successfully. (HiGHS Status 7: Optimal)`; this proves only the fixed final structure.
- No global 159/160 infeasibility claim is made unless all 160 are served.

## Reproducibility

```powershell
cd code
python scripts/12_run_q3_pro.py --run-id q3-pro-deep-v1 --iterations 500 --wall-time 5400 --restart-threshold 60 --master-seed 20260816 --assignment-milp-time 10 --exact-windows 12 --path-pairs 6 --stage2-trials 40 --run-global-bound --promote
```

Detailed convergence, operator, failure, pricing, elite, feedback, exact-LNS and robustness artifacts are in `C:\Users\shiju\.codex\visualizations\2026\08\15\01a00623-03a1-79d1-9a2e-6530cc2c1165\q3-pro-worktree\code\outputs\q3\q3-pro\runs\q3-pro-deep-v1`.

## Verification Status

- Independent Validator: Stage 1 and Stage 2 both valid with zero issues.
- Q3-focused suite: 23 passed.
- Top-level non-recursive suite: 31 passed.
- Full `code/tests`: 77 passed, 4 failed, 2 errors. The non-Q3 failures are
  pre-existing Q1/Q2 artifact issues: missing Q1 relatedness consensus data,
  stale Q1 expected best metrics, Q2 CRLF byte-for-byte comparison, and a
  non-atomic Q2 best/run directory.
- Bare repository `pytest -q` collection is also affected by the checked-in
  duplicate `tests/code/tests` tree causing import-file-mismatch. No unrelated
  Q1/Q2 files were modified to hide these failures.

## Remaining limitations

- The global Stage 1 gap remains wide because the strongest global relaxation drops several routing and scheduling integrality features.
- Stage 2 optimality is certified only for the fixed final flight structure; unrestricted 159/160 feasibility remains open when the incumbent is below 160.
- Lagrangian/Benders are retained as research directions because the higher-priority ALNS, exact LNS, recombination and pricing pipeline consumed the useful search budget.
