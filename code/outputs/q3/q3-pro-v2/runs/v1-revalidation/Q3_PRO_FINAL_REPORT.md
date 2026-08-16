# Q3 PRO Final Report

## Repository

- Source `origin/platinumist_update`: `e0041235f5d11f36e0af1a6f4f680a8b5f6d6b57`
- Q3 PRO run commit: `4e42a986b37fdaef45d7861402639a7321eabac5`
- Run: `v1-revalidation`

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

- Trusted baseline Stage 1: 29155 min.
- Q3 PRO final Stage 1: 29155 min.
- Net Q3 PRO improvement: 0 min.
- Long ALNS iterations: 1; global improvements: 0; restarts: 1.
- Exact/fix-and-optimize windows: 0.
- Path-relink attempts: 0.

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
python scripts/12_run_q3_pro.py --run-id v1-revalidation --iterations 1 --wall-time 60.0 --restart-threshold 1 --master-seed 20260816 --exact-windows 0 --stage2-trials 1
```

Detailed convergence, operator, failure, pricing, elite, feedback, exact-LNS and robustness artifacts are in `outputs\q3\q3-pro-v2\runs\v1-revalidation`.

## Remaining limitations

- The global Stage 1 gap remains wide because the strongest global relaxation drops several routing and scheduling integrality features.
- Stage 2 optimality is certified only for the fixed final flight structure; unrestricted 159/160 feasibility remains open when the incumbent is below 160.
- Lagrangian/Benders are retained as research directions because the higher-priority ALNS, exact LNS, recombination and pricing pipeline consumed the useful search budget.
