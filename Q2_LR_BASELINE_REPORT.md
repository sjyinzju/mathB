# Q2 Logistic Regression Baseline

The leakage-safe Logistic Regression uses standardized continuous features, stable
categorical one-hot encoding and class weighting. IDs, hashes, lineage/run fields
and post-evaluation outcomes are excluded.

## Held-out lineage results

| Ranker | Test PR-AUC | ROC-AUC | Precision@10/25/50 | Recall@10/25/50 | Novel recall@10/25/50 |
|---|---:|---:|---|---|---|
| Random | 0.007778 | 0.525169 | .00699/.00735/.00816 | .0556/.1462/.3246 | 0/0/.1667 |
| Geometry | 0.010279 | 0.654827 | .00368/.00309/.00404 | .0292/.0614/.1608 | 0/0/0 |
| **LR** | **0.036635** | **0.878291** | **.04559/.03750/.02324** | **.3626/.7456/.9240** | 0/0/.1667 |

K is applied inside actual candidate decision groups, not as a single global slice.
At K=10 LR produces 6.46x lift over test prevalence and hits 93.0% of positive
groups. Its Brier score is 0.106894. In the geometry-top/mid hard subset, only five
test novel positives exist; LR novel recall is .4/.8/1.0 at K=10/25/50 versus
geometry .6/.6/1.0. This subset is informative but statistically fragile.

## Feature diagnostics

Largest standardized associations include aircraft-type context, positive flow
complementarity, negative LAND-flexible and inbound-flow terms, positive geometry
score, positive outbound flow, negative route distance, incumbent visibility,
airport distance and supported demand. These are correlations used for ranking,
not causal effects.

## Gate

LR clearly passes the predictive-signal gate: held-out PR-AUC, useful-candidate
Top-K enrichment and group hit rate materially exceed geometry. Novel-positive
evidence is mixed, so LR is deployed only after geometry screening with safeguard
and exploration slots.
