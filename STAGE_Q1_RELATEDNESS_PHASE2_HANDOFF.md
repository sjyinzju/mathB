# Q1 Relatedness Phase-2 交接

## 研究转向与实现范围

Phase-1 已证明 PAM/average hard clustering 不能稳定改善 Savings 排序；全候选负控制回到同一解，raw distance 本身很强。Phase-2 因此不再调 K，而把设施结构改写成可连续评分、可消融的 soft relatedness，并另设依赖当前解的 optimization context。所有模型只排序候选，不删除跨簇候选；Evaluator、SolverCache、技术经停、Validator、规则和目标层级未改。本轮只在现有 Savings ranker API 上做受控下游 A/B，没有进入 ALNS。

新增两层接口：

- `StaticRelatednessModel.relatedness(facility_i, facility_j)` / `relatedness_to_route(...)`：solution-independent，支持 distance、consensus、airport、fuel、capacity 单独启用和等权 rank aggregation。
- `ContextCompatibility.facility_to_route(...)` / `route_pair(...)`：使用 route slack、capacity fill、停靠余量、LAND/固定机场约束、source elimination 和静态 relatedness 的 cheap guidance；不调用 exact evaluator。
- `StaticRelatednessRanker` / `ContextRelatednessRanker`：供 Savings 使用，同时接口不依赖 Savings 专用对象，可供未来 related destroy/repair 调用。

## Static components 与 consensus

所有连续静态分量均为 1,326 个设施对上的经验百分位相似度，范围 `[0,1]`，保留单分量矩阵和 pair-level 审计表。

- **Distance**：题面直接距离，不做 metric closure。
- **Consensus**：读取 Phase-1 已保存且通过稳定性/无单点簇门槛的 13 个 PAM/average 配置，以 median ARI 加权 co-association；没有重跑 K 网格。矩阵有 65 个不同 pair 值，leave-one-configuration-out 平均绝对变化最大仅 0.02395，作为连续结构描述明显比单一 K 稳定。
- **Airport affinity**：三个机场距离轮廓的平均绝对差，经 percentile 转为相似度，不硬指定机场。
- **Fuel topology**：复用 `closed_route_reachability.csv` 的 9 维机场×机型最少技术经停/是否必须加油签名，不做 exact route reconstruction。
- **Demand/capacity**：用设施总需求和 12/16/19 座容量计算分开服务与合并服务的最小总座位、saved seats 和 combined utilization，再对两项 rank-average。它保持 solution-independent，但不能看到当前 batch/route slack。

## Offline ranking gate

标签来自 Phase-1 `raw-full`：10 个 Savings 迭代、776 个全候选 pair 均经过 exact evaluator，有真实 saving；没有把 feasibility 当主任务。指标为 per-iteration Recall/NDCG/high-saving coverage@25/@50、best-move hit 和 Spearman，最后取 10 轮均值。

| 模型 | NDCG@25 | Recall@25 | Coverage@25 | NDCG@50 | Spearman |
|---|---:|---:|---:|---:|---:|
| raw distance | **0.8894** | **0.660** | 0.9047 | **0.9116** | **0.5504** |
| hard PAM K3 | 0.8818 | 0.628 | 0.8941 | 0.9035 | 0.5383 |
| distance + consensus | 0.8814 | 0.656 | **0.9105** | 0.9030 | 0.5191 |
| distance + airport | 0.8807 | 0.644 | 0.9047 | 0.8885 | 0.4404 |
| distance + fuel | 0.8799 | 0.652 | 0.9033 | 0.8956 | 0.4394 |
| distance + capacity | 0.8716 | 0.620 | 0.8989 | 0.8890 | 0.4114 |
| full equal-rank static | 0.8871 | 0.644 | 0.9027 | 0.9019 | 0.4713 |
| distance + context | 0.8469 | 0.584 | 0.8781 | 0.8820 | 0.4559 |

决策：distance **ADOPT/control**；consensus **OPTIONAL（仅 Coverage@25 +0.0058，但 NDCG 下降、仅 3/10 轮胜）**；airport **OPTIONAL/不进入当前 ranker（coverage 变化近零，2/10 轮胜）**；fuel、capacity **REJECT**。没有新增 component 通过稳定 ADOPT gate，因此“最佳 Static”诚实退化为 distance-only，而不是强行保留 full model。

原计划高优先级研究 capacity，但设施总需求 19–51 人、当前候选实际由动态 route load/slack 决定；静态总需求 capacity 分量 NDCG@25 比 raw 低 0.0178，是本轮最弱增量。结论是 capacity 应留在 context 层，不应继续优化静态 facility component。

## Controlled downstream A/B

三种 ranker 从同一 B0、同一 global pair budget=25 出发；每次使用独立的新 `SolverCache`，Evaluator、objective、停止规则完全相同。全部 1600/1600、内部指标一致、Validator PASS。

| Ranker | 飞机时间 | 人员时间 | 架次 | 燃油/kg | pair eval | route eval | search time | time-to-first | time-to-best |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| raw distance | **15,683** | 120,949 | 99 | **128,016.7** | 226 | 636 | 9.82s | 0.253s | 9.82s |
| best static (=distance) | **15,683** | 120,949 | 99 | **128,016.7** | 226 | 636 | 10.44s | 0.260s | 10.44s |
| static + context | 15,693 | **120,932** | 99 | 128,473.3 | 234 | **562** | **8.85s** | **0.164s** | **8.84s** |

Context 将 route evaluations 降低 11.6%、search time 降低约 10.0%，更早找到首次改进且 improvement/evaluation 从 2.42 升至 2.72；但最终飞机时间恶化 10 分钟，高价值（≥100 min）候选从 106 降至 96。它是“效率富集但质量未过门”的弱信号，不能替代 raw ranker，也没有产生比 15,371 strong classical benchmark 更好的 Q1 解。

## 测试、产物与下一步

全量 54 项测试通过，包括 static deterministic/symmetry/self、consensus reproducibility、airport/fuel 单调一致、capacity breakpoint、route slack 响应、固定机场/LAND 语义、ranker deterministic，以及原有 legacy/Performance Core 差分测试。三条下游输出均重新导出官方 CSV并独立 Validator PASS。

完整产物：`outputs/q1/relatedness/20260815-phase2/`，含 component pair table、consensus diagnostics、offline iteration/summary、ablation、三组下游 candidate logs、cache/performance、CSV、Validator 和 config。

建议：**停止继续优化 clustering/K 或静态 composite。当前证据不足以启动专门的 Relatedness-aware ALNS 融合。** 等 Standard ALNS 算子与奖励日志稳定后，可做两个独立 ablation：related destroy 调用 `StaticRelatednessModel.relatedness`（以 distance control、consensus optional）；repair priority 调用 `ContextCompatibility.facility_to_route`，重点保留 capacity/slack 与机场合法性，并始终保留 cross-related exploration。当前 `ContextRelatednessRanker` 不应直接晋升。

尚未解决风险：offline 日志仅来自一条 raw Savings 轨迹；context offline 特征受 Phase-1 日志字段限制，无法包含真实 route duration/aircraft type；下游计时只运行一次；capacity 静态定义使用设施总需求而非动态 batch。这些风险应在 ALNS 有稳定候选/奖励日志后再复核，而不是现在扩大搜索。
