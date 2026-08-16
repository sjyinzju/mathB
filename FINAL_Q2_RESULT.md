# B 题 Q2 最终冻结结果

## 最终 validated solution

正式 CSV 位于 `outputs/q2/best`，其 immutable 副本位于
`outputs/q2/final-ml/final-17076`；Round-3 classical control 保存在
`outputs/q2/final-ml/frozen-control-17218`。

| 指标 | 最终值 |
|---|---:|
| Aircraft time | **17,076 min** |
| Passenger time | **253,414 min** |
| Flights | **94** |
| Fuel | **131,721.5 kg** |
| Utilization | **0.9305132229** |
| Served | **4,000 / 4,000** |
| Validator | **PASS, 0 issues** |

相对冻结的 Round-3 `17,218 / 95` control，aircraft time 降低 142 分钟
（0.8247%），passenger time 降低 1,242 分钟，减少 1 个航班，fuel 降低
752.4 kg。相对 canonical 19,736，aircraft time 降低 2,660 分钟（13.4779%）。

## Winning algorithm

最终算法是 classical ALNS backbone 上的 leakage-safe Logistic Regression
候选重排：fixed 4-route neighborhood、rare targeted 5-route、geometry coarse
screening、2 个 pure-geometry safeguard slots、1 个 deterministic exploration slot、
Shared Solver Core 与 Exact Local MILP Repair。ML 只分配候选 exact-evaluation
预算，不改变可行性、目标函数、MILP repair 或 Validator。

第一段 LR extended search 从 17,218 降至 17,107，并自然完成 95→94；第二段
从 17,107 降至最终 17,076。相同起点、seed 和搜索设置的第二段 geometry
对照止于 17,085。因此严谨结论是：ML ranking 帮助搜索进入更好的候选轨迹；
不能声称 ML 直接生成了最终路线。

## Frozen hashes

- routes: `9366573BD7C70A5D03533DBD6FDA73231B413FCACCE884F987AE0E9AA278E830`
- assignments: `3E61EE375DB7FC792E90E4E14CACEA647912B607F103A3328542AEF2645B73FB`

Q2 至此正式冻结，不再开启 generic 6-route、repeated visits、column generation、
Branch-and-Price、deep learning 或新的大规模参数搜索。下一步进入 Q3。
