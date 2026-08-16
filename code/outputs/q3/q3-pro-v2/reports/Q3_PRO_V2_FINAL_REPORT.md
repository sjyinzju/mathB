# Q3 Pro V2 Deep Optimization — Final Report

## Executive result

The authoritative V2 incumbent is feasible under the repository's independent Q3 Validator.

| Objective layer | Aircraft time | Mandatory served | Optional served | Flights | Passenger time | Fuel | Seat utilization | Validator |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Stage 1 | **28,728 min** | **3,840 / 3,840** | — | 162 | 242,455 min | 226,835.3 kg | 0.5784037253 | valid, 0 issues |
| Stage 2, Stage-1 cap fixed | **28,728 min** | **3,840 / 3,840** | **157 / 160** | 162 | 251,777 min | 226,835.3 kg | 0.6009846380 | valid, 0 issues |

The three unserved optional passengers are `P1102`, `P2239`, and `P3290`.

## Improvement and attribution

| Reference | Aircraft time | V2 reduction |
|---|---:|---:|
| Q3 Pro V1 validated incumbent | 29,155 | **427 min** |
| Earlier 29,659 solution | 29,659 | **931 min** |
| Historical 30,546 solution | 30,546 | **1,818 min** |

The 427-minute V1-to-V2 gain decomposes into:

- heterogeneous screen/deep ALNS, dominated by Cross-Day moves: 29,155 → 28,868 (**−287 min**);
- Optional Rescue plus mandatory-only P0 feedback projection: 28,868 → 28,728 (**−140 min**), while the Stage-2 optional count remained 157.

Across the 40 screen runs, 22 finished at 29,155, 13 at 29,006, and 5 at 28,868. Cross-Day made 23 accepted screen moves from 73 attempts. Across screen and deep phases combined, the search attempted 766 operators, accepted 29 moves, and recorded 23 global-best events; Cross-Day accounted for 28 accepted moves and all 23 global-best events. Route polish accounted for the remaining accepted move.

## Search budget and evidence

- Formal run ID: `q3-pro-v2-deep-v1`.
- Wall time: 9,763.627128 seconds (162.73 minutes).
- Parameter screen: 20 heterogeneous configurations × 2 seeds × 20 iterations, 60-second per-run guard.
- Deep phase: 4 islands × up to 500 iterations, 900-second per-island guard.
- Guided exact LNS: 50 windows, all reached their bounded wall guard, 0 accepted improvements, 1,876.936 seconds. This is finite-neighborhood evidence, not a global proof.
- Local branching: radii 5/10/20/40/80, 0 accepted improvements.
- Aircraft-day chain search: 10 windows, 0 accepted improvements.
- Optional Rescue: depths 2–8, L1–L4 and groups up to 12; 1 accepted structural move, 13 bounded timeouts, then a second rescue pass after P0 feedback.
- Pricing: 50 iterations; 363 selected routes imported into the primal pool. The restricted-pool LP value was 17,217.4167 and the full finite-pool LP value was 15,197.6776; neither is a valid global lower bound.
- Recombination/path relinking was skipped by the specified minimum-diversity gate because only 11 eligible elites existed at that point.

Persistent search assets contain 12 unique elites (mean pairwise distance 0.0519357), 3,116 route variants, and 179 deduplicated flight columns from 12 schedule sources. The requested 20–50 elite target was not reached and is reported without inflation.

## Bounds and optimality status

The layered continuous multicommodity-flow relaxation was rebuilt and solved to optimality:

- continuous lower bound: 14,124.8947368421 minutes;
- valid integer-ceiling lower bound: **14,125 minutes**;
- best feasible upper bound: **28,728 minutes**;
- certified relative gap: **50.8319409635%**, computed as `(28,728 − 14,125) / 28,728`.

Therefore Stage 1 is **not claimed globally optimal**. For Stage 2, the assignment MILP proves 157 is optimal on the final fixed flight structure. Under the 28,728-minute Stage-1 cap, unrestricted 158/159/160 feasibility remains open; no global infeasibility certificate is claimed.

The optional-rescue dossier identifies a critical-leg seat blocker for each remaining passenger. `P1102` and `P3290` share OD `F009 → F003`; `P2239` has OD `LAND → F035` and three compatible bases. These are diagnosis results, not impossibility proofs.

## Robustness

The robustness runner rebuilt route universes and attempted schedule reconstruction under seven structural perturbations: turnaround 40/45 minutes, removal of one `A03-T2`, loss of refuelling at `F038`, flight times +5%/+10%, and time windows tightened by 30 minutes. No feasible reconstruction was found in the bounded constructor for any of the seven. Every record explicitly sets `global_infeasibility_proof=false`; these outcomes are search failures, not infeasibility certificates.

For temporary demand −10%, the final fixed structure remained feasible, serving 141 of the 144 retained optional passengers and leaving the same three IDs unserved.

## Validation and regression tests

- Independent Stage-1 Validator: valid, 0 issues.
- Independent Stage-2 Validator: valid, 0 issues.
- Q3-focused suite including V2: **31 passed**.
- Repository-root top-level suite: **31 passed**.
- Full `code/tests` suite: **85 passed, 4 failed, 2 errors**. The six non-passing cases are pre-existing and outside Q3 V2: missing `q1-relatedness-consensus.csv` (2 errors), Q1 checked-in/expected objective and short-improvement assertions (2 failures), and Q2 CRLF/atomic-directory assertions (2 failures). Q1/Q2 files were not modified.

## Repository provenance

- Isolated branch: `codex/q3-pro-v2`.
- V1 source commit: `4e42a986b37fdaef45d7861402639a7321eabac5`.
- Formal-run code commit: `b71dad431a1a89490694c78afd3efc69baf0b307`.
- Earlier V1 run-code commit independently verified: `643a4c784c277ab74122033eeb70b74177d4045e`.
- At report generation, the validated result tree and this report were present in the isolated worktree but **not committed**, because the platform rejected Git-metadata write escalation after its approval-credit limit was reached. No permission bypass was attempted.
- The original `D:\Desktop\B题` worktree and the V1 worktree were not changed; no merge, push, or canonical V1-output overwrite was performed.

## Authoritative artifacts

- Final Stage 1: `current_incumbent/q3-base-routes.csv`, `current_incumbent/q3-base-assignments.csv`, `current_incumbent/q3-base-validator.json`.
- Final Stage 2: `current_incumbent/q3-routes.csv`, `current_incumbent/q3-assignments.csv`, `current_incumbent/q3-validator.json`.
- Metrics and bounds: `current_incumbent/metrics.json`, `current_incumbent/bounds.json`.
- Formal run snapshot: `runs/q3-pro-v2-deep-v1/`.
- Convergence and portfolio: `parameter-screen.csv`, `deep-islands.csv`, `convergence.csv`.
- Diagnostics: `bottleneck-report.json`, `optional-rescue-dossier-v2.json`, `final-feedback.json`, `secondary-polish.json`.
- Robustness: `robustness-v2.json`, `runs/robustness/`.
- Persistent assets: `elite_pool/v2-final/`, `route_library/`, `column_library/`, `checkpoints/`.

## Reproduction

From `code/` at formal-run commit `b71dad4`:

```powershell
python scripts/13_run_q3_pro_v2.py --run-id q3-pro-v2-deep-v1 --screen-configs 20 --screen-seeds 2 --screen-iterations 20 --screen-wall-time 60 --deep-islands 4 --deep-iterations 500 --deep-wall-time 900 --optional-trials 5 --exact-windows 50 --exact-time-limit 5 --aircraft-day-windows 10 --recombination-pairs 20 --pricing-iterations 50 --run-global-bound
python scripts/14_run_q3_pro_v2_robustness.py
```

Because both runners persist intermediate state, use a fresh run ID/output root when reproducing rather than overwriting the authoritative artifacts above.
