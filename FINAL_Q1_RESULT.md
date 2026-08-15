# Q1 最终结果

## 最终结论

最终采用 **Standard ALNS A2 extended**。从全部独立验证通过的候选中按严格词典序选择，最终总飞机使用时间为 **14,770 分钟**，服务 **1,600/1,600** 人，Validator **VALID（0 issues）**。

| 指标 | 最终值 |
|---|---:|
| 总飞机使用时间 | 14,770 min |
| 人员总在途时间 | 120,845 min |
| 总架次 | 89 |
| 总燃油 | 118,640.1 kg |
| 座位利用率 | 0.493389 |
| 安排人数 | 1600/1600 |

相较 B0 的 17,222 分钟减少 **2,452 分钟（14.24%）**；相较 B1 的 15,743 分钟减少 **973 分钟（6.18%）**；相较 Classical VND 的 15,371 分钟减少 **601 分钟（3.91%）**；相较 Standard V1 300s best 15,118 减少 **348 分钟（2.30%）**；相较阶段开始时 absolute incumbent 15,052 减少 **282 分钟（1.87%）**。

## Relatedness 融合判断

300 秒公平基准由 **Distance Destroy + Context-160（R3）** 获胜：best/median/mean/worst 为 **15025/15149/15142.0/15288**；Standard V1 为 **15118/15185/15208.4/15281**。R3 改善 best 93、median 36、mean 66.4 分钟，同 seed 赢 4/5，但 worst 多 7 分钟，因此质量总体更强而非方差全面更优。

Distance-only 正式公平结果为 best/median **15167/15205**，未超过 Standard V1，故不单独晋升。Context-only 虽把 screening evaluator calls 平均减少 **56.2%**，但 best/median 恶化至 **15329/15344**，也不单独晋升。R3 在 screening 中保持近似质量并减少 evaluator calls **57.0%**，说明 Context 的价值是与 distance 联合后的候选预算效率。

保留：raw distance destroy、可解释 Context V2 排序（geometry/capacity-slack/ejection/airport/route-state）、exact repair/Evaluator/Validator 唯一裁决。淘汰：consensus（均值/最差无稳定增益）、fuel static、static capacity、full static composite、hard clustering；Bandit 未进入主线。

Relatedness extended 将 15,025 继续降至 **14,772**，确实刷新 final best；随后纯 Standard A2 热启动再降至当前最终值。因此 Relatedness 是 300s 公平预算 winner 和突破旧 incumbent 的关键搜索阶段，但最终 CSV 的最后一步 winner 由 `Standard ALNS A2 extended` 产生。

## 可复现产物

- `outputs/q1/final/q1-routes.csv`
- `outputs/q1/final/q1-assignments.csv`
- `outputs/q1/final/metrics.json`
- `outputs/q1/final/validator.json`
- `outputs/q1/final/winning_config.json`
- `Q1_FINAL_COMPARISON.csv`

最终独立 Validator 为 VALID/0 issues，内部指标与 Validator 一致；全量 **54 tests PASS**，`git diff --check` 通过。
