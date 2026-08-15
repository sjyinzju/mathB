# Q1 Relatedness × Standard ALNS 最终交接

## 基座与阶段门

- 融合分支：`codex/q1-relatedness-alns`；基座 commit：`7ddd2f7`（`platinumist_update_alns_base`）。没有 merge/cherry-pick clustering 分支，仅移植冻结数据与适配逻辑。
- Standard 公平控制仍为 A3/V1 300s、seeds 0–4：best 15,118，median 15,185，mean 15,208.4，worst 15,281，全部 VALID。
- 开始时 absolute validated incumbent 仍为 A2 seed 4 的 15,052。
- Relatedness disabled 的 fixed-iteration no-op regression 保持相同 15,361 结果、4 iterations、687 evaluator calls；测试覆盖 legacy 退化和新排序确定性。

## 五阶段结果

1. No-op：PASS，默认配置不改变 Standard ALNS 语义。
2. Distance：用 raw route distance 替换 legacy airport/fixed-origin penalty。正式 300s best/median 15,167/15,205；稳定性较好但中心质量未胜 V1，单独 REJECT。
3. Consensus：只做一次 soft A/B；screen best 15,255，但 mean/worst 变差，REJECT。
4. Context V2：在 exact candidate build/MILP 前做 explainable rank/budget。Context-only evaluator calls 降 56.2% 但质量恶化，单独 REJECT。
5. Combined R3：screen evaluator calls 降 57.0% 且质量近似；正式 300s best/median/mean 15,025/15,149/15,142.0，同 seed 4/5 胜 V1，ADOPT 为 fair-budget winner。

所有 guidance 只做 ranking/pruning；LegPhysics、SolverCache、exact repair、Evaluator、objective、Exporter、Validator 未被替换。保留 raw distance 与 Context V2；淘汰 consensus、静态 fuel/capacity/full composite、hard partition。

## Extended 与最终解

- Relatedness R3：15,025 → 14,791 → **14,772**，均 VALID。
- Standard A2 从 14,772 热启动：**14,770**，runtime 604.658s，time-to-best 82.207s，VALID。
- Standard A3 从 14,770 热启动：**14770**。
- 最终 winner：**Standard ALNS A2 extended，14770 分钟**；人员时间 120845，89 架次，燃油 118640.1 kg，利用率 0.493389，1600/1600，Validator VALID/0 issues。

## 产物与验证

正式原子输出位于 `outputs/q1/final/`；比较表为 `Q1_FINAL_COMPARISON.csv`；答题总结为 `FINAL_Q1_RESULT.md`。最终 CSV、metrics、validator、winning config、method metadata 全部来自同一 winning run。最终独立 Validator VALID/0 issues，内部指标一致；全量 54 tests PASS，`git diff --check` 通过。Q1 到此停止，不进入 Q2/Q3。
