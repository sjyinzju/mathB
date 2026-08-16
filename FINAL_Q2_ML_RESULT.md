# Q2 Final ML-Guided Optimization

## Outcome

Decision: **ADOPT** the leakage-safe LR-guided ranker and freeze Q2 at
`17,076 / 253,414 / 94 / 131,721.5 / 0.9305132229` with 4,000/4,000 served and
independent Validator PASS (0 issues).

The Round-3 `17,218 / 95` control remains immutable. The final result improves the
primary objective by 142 minutes (0.8247%), passenger time by 1,242 minutes, flights
by one, and fuel by 752.4 kg.

## Evidence chain

1. Dataset V2 was recomputed from candidate events: 663,504 rows, 309,785 exact
   evaluations including invalid cases, and 308,767 exact supervised rows.
2. Run/parent-chain lineage isolation produced three disjoint roots. CENSORED was
   never relabeled negative; INVALID was excluded from the useful-candidate model.
3. On the held-out test lineage LR achieved PR-AUC 0.036635 versus geometry
   0.010279 and random 0.007778. At per-group K=25 it recalled 74.56% of positives
   versus 6.14% for geometry.
4. LightGBM classifier gave the strongest validation PR-AUC (0.028271), but the
   novel-positive sample was sparse and online short-run evidence did not justify
   using it for extended search. LR was retained as the simpler, more stable ranker.
5. The 48-run online A/B covered three basins, two seeds and both matched
   exact-evaluation and wall-clock budgets. Short runs did not beat 17,218, but LR
   improved the independent basin from 17,391 to 17,381 in matched-budget runs and
   increased useful-repair yield.
6. The gated LR extended search reached 17,107/94, while matched geometry remained
   at 17,218/95. A second matched restart reached 17,076; geometry reached 17,085.

## Runtime evidence

| Run | Policy | Start | Final | Exact evals | Runtime | Time to best |
|---|---|---:|---:|---:|---:|---:|
| extended-s731 | LR | 17,218 | 17,107 | 90,905 | 1,641.40 s | 1,552.85 s |
| control-s731 | Geometry | 17,218 | 17,218 | 7,978 | 187.08 s | 26.09 s (secondary) |
| restart-s732 | LR | 17,107 | **17,076** | 32,357 | 688.14 s | **542.09 s** |
| control-s732 | Geometry | 17,107 | 17,085 | 20,799 | 262.23 s | 210.01 s |

LR inference consumed 29.39 s and 10.92 s in the two extended runs (about 1.8% and
1.6% of runtime). Bounds and gaps remain restricted-local-master diagnostics, not
global Q2 optimality claims.

## Causal wording

The defensible statement is: **ML-guided candidate ranking improved exact-evaluation
allocation and unlocked a better validated ALNS trajectory.** Route feasibility,
aircraft physics and the final construction remained authoritative classical
optimization components.
