# B 题 Q2 最终结果说明

## 1. 最终算法

最终采用“**Elite-recombined SA Adaptive ALNS + Exact Local MILP Repair**”。
算法以 main Shared Solver Core 为唯一物理与规则基础，在当前解中选择 4 条相关架次，
生成 geometry-ranked bounded 1–5-stop 候选，用局部整数规划精确重排所有受影响人员。
四类 destroy operator 由 adaptive roulette 选择；SA 允许主目标相同的结构性过渡；
多个 validated local optima 进入小型 diverse elite pool，并只对差异区域做 exact
recombination。最终没有采用未经实验支持的复杂组件。

选择该算法的原因是：4-route exact neighborhood、SA 与 elite recombination 都在
公平 A/B 中改善 best 和 median；local masters 能在受限候选池上求到 gap 0；所有
候选仍经过 Shared Solver Core 和独立 Validator，算法增强没有改变规则语义。

## 2. 正式结果

最终解位于 `outputs/q2/best`，源 immutable run 为
`outputs/q2/runs/20260815-q2-final-repro-s2`；它在代码 checkpoint
`da0d87d` 上逐字节复现正式 seed 2 的 routes、assignments 与 metrics。

| 项目 | 最终值 |
|---|---:|
| 已安排人员 | **4,000 / 4,000** |
| 飞机总使用时间 | **17,958 min** |
| 人员总在途时间 | **263,588 min** |
| 总架次数 | **97** |
| 总燃油消耗 | **138,075.2 kg** |
| 座位利用率 | **0.8974631474** |
| 独立 Validator | **PASS，0 issues** |

五种子正式终验的 aircraft time 为
18,010、18,048、17,958、18,043、18,102；best/median/mean/worst/std 为
17,958/18,043/18,032.2/18,102/47.4063，5/5 均低于 18,906 control。

## 3. 相对改善

| 对照 | Aircraft time | 改善 | Passenger time | Flights | Fuel | Utilization |
|---|---:|---:|---:|---:|---:|---:|
| Separate baseline | 44,184 | -26,226 (-59.36%) | +3,067 | -161 | -215,918.6 | +0.5508550 |
| Canonical RMP | 19,736 | -1,778 (-9.01%) | -7,146 | -10 | -14,835.2 | +0.0791649 |
| Standard ALNS control | 18,906 | -948 (-5.01%) | -2,720 | -6 | -8,367.3 | +0.0437596 |

Aircraft time 是绝对第一目标。Separate baseline 的人员在途时间比最终解低 3,067
分钟，但其飞机时间高 26,226 分钟、架次数高 161，因此不构成主目标上的优胜。

## 4. 组件结论

- Route ejection：**有效并保留**；最终 winner 仍删除 1 条架次。
- Larger neighborhood：**有效**；fixed 4-route 明显胜 3-route。
- Adaptive destroy size：**淘汰**；当前 2/3/4 调度规则的中位数更差。
- Multi-route ejection chain：**机制有效但淘汰为默认**；能删路线，质量不胜普通
  4-route exact repair。
- Elite recombination：**有效并保留**；多组 partner 稳定改善 incumbent。
- SA：**有效并保留**；通过等主目标结构变化提高后续解质量。
- Context-aware ranker：**弱信号 / OPTIONAL**；单次 best 更好但 median 更差，默认
  使用 geometry。
- 4-stop：**出现真实 accepted evidence**；最终五种子共选择 6 条新 4-stop。
- 3-stop：**继续有效**；最终共选择 5 条新 3-stop。
- 5-stop：保留早期真实 selected evidence，本轮终验没有新增采用。
- Performance local cache：**淘汰**；语义一致但仅 1.56% 提速。
- ML：**未训练**；数据按 run 分组后不足，避免选择偏差和伪负样本。
- UCB Bandit：**淘汰**；不及 adaptive roulette。
- Repeated visits：**暂不值得进入**；没有足够结构证据支持 occurrence 改造成本。
- Heuristic Column Generation：**继续 REJECT**。
- Full Branch-and-Price：**继续 REJECT**。
- Deep Learning / GNN / Transformer / Deep RL：未进入。

## 5. 合法性与可复现性

最终 aggregate allocation 已分解为具体人员架次，导出 official CSV 后由独立
Validator 重算：4,000 人恰好覆盖一次、0 issues，五项内部指标与 Validator 完全
一致。每个 run 记录 source SHA、Python/SciPy/HiGHS、配置、候选日志、restricted
local master 状态和 SolverCache 统计。局部 gap 仅代表 finite
`restricted_local_master`，不宣称 Q2 全局最优。

正式结果文件：

- `outputs/q2/best/q2-routes.csv`
- `outputs/q2/best/q2-assignments.csv`
- `outputs/q2/best/q2-validator.json`
- `Q2_FINAL_COMPARISON.csv`
- `outputs/q2/final-benchmark.csv`
