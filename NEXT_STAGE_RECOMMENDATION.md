# Q2 Next Stage Recommendation

## Decision: CASE A — enter ML

Round-3 classical search 已将 Q2 从 17,595 / 96 降至 17,218 / 95，并在多个 basin
进入 stagnation。与此同时，ML dataset V2 已有 89 个 novel positives，分布于三个
parent-chain lineage groups，train/validation/test 均有 novel coverage。因此下一阶段
进入受控的 ML-guided candidate ranking，而不是再扩大 classical neighborhood。

顺序必须是：

1. Logistic Regression：exact-evaluated 子集上的可解释 baseline；
2. LightGBM ranking：同 split、同 exact budget、同 ALNS start/seed 做 A/B；
3. Random Forest 仅 optional。

首要 target 是 useful accepted MILP-selected candidate 的概率；CENSORED 不得作为
negative。模型只能改变 candidate priority/exact-evaluation allocation，不能改变 Shared
Solver Core、feasibility、Validator 或 primary objective。

继续 REJECT：repeated visits、generic 6-route、heuristic Column Generation、Full
Branch-and-Price、GNN、Transformer、Deep RL。若 ML A/B 无稳定增益，冻结 Q2，不再同时
开启多条 OR 路线。
