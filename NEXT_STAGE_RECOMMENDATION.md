# Q2 Next Stage Recommendation

## Decision

当前不进入 ML training，也不立即冻结 Q2。建议再做一次严格有界的 classical OR：

**96→95 Flight Absorption Intensification**

若该阶段没有 route elimination 或持续 primary 改善，再冻结 Q2 并进入后续题目。

## Required questions

1. Round-2 final best：17,595 min，96 flights。
2. 相比 17,958：改善 363 min（2.02%）。
3. 是否仍发现新结构：是；extended search 在约 110 秒出现一次 4→3 route repair。
4. Flights 能否继续下降：未知；97→96 已成功，96→95 尚无证据。
5. Elite/path relinking：质量受限 recombination 有效；远距离 partner 成本过高。
6. Local branching/fix-and-optimize：当前实现均不值得继续；若重开应换窗口/representation，
   不能简单重复本轮。
7. Repeated visits：当前无具体 high-value evidence，继续 REJECT。
8. Candidate space 是否瓶颈：部分是；最终 elimination 使用两条新 3–4-stop columns，
   但多数 useful positives 仍是 incumbent columns。
9. Exact repair 是否瓶颈：是局部吞吐瓶颈；最终关键 repair 14.98 秒，restricted gap
   5.865%，但无需大规模性能重构。
10. ML dataset READY：NOT_READY。
11. 最可信 label：exact-evaluated 中 useful accepted MILP-selected candidate。
12. ML 最值得先做：等 novel positives 足够后，Logistic Regression，再 LightGBM ranking。
13. 若不做 ML，下一 OR：96→95 low-utilization/shared-facility exact absorption windows，
    结合 global-best/quality-elite restarts。
14. 是否冻结 Q2：现在不冻结；再给一个 bounded stage，失败后冻结。

## Why one more classical stage

最终 improvement 很晚出现，且一次下降 61 分钟并删除整架次，说明 96-flight incumbent
是新的结构状态，而非旧 97-flight plateau 的简单小修。另一方面，此后 19 repairs、约
72 秒无 primary improvement，不能无限续跑。因此下一阶段应只围绕新结构做少量高潜力
windows，而不是全面重启所有淘汰方向。

## Explicitly remain rejected

- repeated-service occurrence 改造，除非先发现多个具体 96→95 cases；
- heuristic column generation；
- full Branch-and-Price；
- UCB Bandit；
- 未达到 readiness 前的 LR/RF/LightGBM；
- Deep Learning、GNN、Transformer、Deep RL。
