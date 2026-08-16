# Q2 ML Dataset V2 Audit

| Item | Recomputed value |
|---|---:|
| Candidate rows | 663,504 |
| Exact evaluated, including INVALID | 309,785 |
| Exact supervised (POSITIVE + TRUE_NEGATIVE) | 308,767 |
| POSITIVE | 1,863 |
| Novel POSITIVE | 89 |
| TRUE_NEGATIVE | 306,904 |
| CENSORED | 353,719 |
| INVALID | 1,018 |
| Duplicate candidate IDs | 0 |
| Parent-chain lineage roots | 3 |

The lineage split is isolated and has no run/parent-chain overlap. Train,
validation and test contain respectively 495/1,026/342 positives and 26/51/12
novel positives. Identity fields (candidate/run/solution hashes, lineage IDs and
seed) are excluded from model features. No outcome, acceptance, selected or
future-objective feature was detected.

CENSORED rows are not negatives. INVALID rows remain available for separate
diagnostics but are excluded from the useful-candidate target. The primary label is
exactly `POSITIVE=1`, `TRUE_NEGATIVE=0`.

## Source audit

| Source | Rows | Exact | Positive | Novel | Exact positive rate |
|---|---:|---:|---:|---:|---:|
| INCUMBENT | 184,384 | 184,384 | 1,774 | 0 | 0.9621% |
| GEOMETRY_TOP | 230,785 | 92,548 | 66 | 66 | 0.0713% |
| GEOMETRY_MID | 107,830 | 1,880 | 2 | 2 | 0.1064% |
| GEOMETRY_LOW | 85,637 | 305 | 0 | 0 | 0% |
| EXPLORATION_RANDOM | 24,536 | 24,536 | 20 | 20 | 0.0815% |
| CROSS_EXCHANGE | 22,238 | 4,398 | 1 | 1 | 0.0227% |
| ABSORPTION | 4,171 | 931 | 0 | 0 | 0% |
| TARGETED_5_ROUTE | 3,923 | 803 | 0 | 0 | 0% |

The 89 novel positives are too sparse for strong universal novel-generalization
claims. This is why geometry safeguards and exploration remain mandatory online.
