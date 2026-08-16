# Q2 LightGBM Report

Two compact, regularized models were trained with lineage-isolated validation and
early stopping: a binary classifier (best iteration 56) and a group-aware ranker
(best iteration 49). There was no large hyperparameter sweep and test data was not
used to select the online model.

| Model | Validation PR-AUC | Test PR-AUC | Test ROC-AUC | Recall@10/25/50 |
|---|---:|---:|---:|---|
| LR | 0.024193 | 0.036635 | 0.878291 | .3626/.7456/.9240 |
| LGBM classifier | **0.028271** | 0.039168 | **0.881986** | .3450/.7602/.9240 |
| LGBM ranker | 0.026474 | **0.041219** | 0.874847 | .3596/.7485/.9181 |

The classifier wins the predeclared validation PR-AUC gate and therefore represents
LightGBM online. The ranker has the highest test PR-AUC, but selecting it after
seeing test would be leakage. Both models have zero global novel recall at K=10/25
and only the classifier reaches .1667 at K=50. On the five-positive hard test subset,
classifier novel recall is .4/.6/.8 and ranker .2/.6/1.0.

Important gain features are incumbent visibility, elite-pool visibility, directed
shuttle flow, current-route passenger count and duration, technical-stop complexity,
utilization, iteration, LAND fraction, novelty and route/airport distances. The
dominance of incumbent-related features plus only three lineage roots is an
overfitting warning.

LightGBM improves offline PR-AUC slightly over LR but does not establish a stable
online advantage. It is retained as an experiment artifact, not the final extended
ranker.
