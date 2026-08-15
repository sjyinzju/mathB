# Q1 Elite Route Pool 报告

## 设计与语义

Route identity 不是 `set(service_nodes)`。身份键包含 base airport、aircraft type、ordered service sequence、physical stop structure、technical-stop/refuel/service semantics，并为完整 allocated pattern 另建 column identity。这样既保留同设施不同顺序的物理差异，也允许同一物理 route 携带不同合法 passenger allocation。

每条记录保存 route/source/algorithm/seed/source objective、base/type、ordered service、physical stops、duration、fuel、capacity、allocation signature、technical stops、合法性、first seen、best membership 和 tier。Master 采用 **exact allocated-route-pattern set partitioning**，不另造一套 physics 或 legality。

## Pool 规模与 pruning

最终成功的 Round 4 pool：

| 项目 | 数值 |
|---|---:|
| source solutions | 95 |
| unique physical routes | 391 |
| allocated route-pattern columns | 996 |
| demand/aircraft constraints | 104 |
| CORE | 128 |
| ELITE | 100 |
| EXPLORATION | 163 |
| collapsed duplicate observations | 8,502 |
| duplicate solutions skipped | 16 |
| low-quality sources skipped | 24 |

Round 5 加入最终 Standard/Relatedness education 后共有 98 sources，物理 route 仍为 391，说明后续 pool 的高质量结构增长已经停滞。

Pruning 保守进行：语义去重、solution 去重、只接收 source objective ≤ 15,371 的有竞争力来源，并以 CORE/ELITE/EXPLORATION 分层控制规模。本阶段没有激进删除长期未选 routes，也没有用简化 coverage 做可能错误的 dominance pruning。

## 来源与闭环

Pool 汇总了 Classical VND、Standard A2/A3、Relatedness、不同 seeds、extended runs、master solutions、targeted neighborhoods、cross-exchange 与 island feedback。逻辑 islands 为 A2、A3、Relatedness、Master warm start 和 structured exchange/relinking；它们共享 pool，而非维护复杂并行框架。

| 轮次 | Pool / search 动作 | best primary |
|---|---|---:|
| Frozen control | Standard A2 extended | 14,770 |
| Reproduction gate | control-only exact master | 14,770 PASS |
| Round 1 | 87 sources / 388 routes / 950 columns | 14,743 |
| Round 2 | flight-cap exact master | 14,732 / 88 flights |
| Targeted | 19 structured neighborhoods | 14,732 |
| Island feedback | Standard seeds 11/12/13 contribute routes | 14,741 / 14,737 / 14,737 |
| Round 4 | 95 sources / 391 routes / 996 columns | **14,730** |
| Education | Standard ALNS warm start | **14,730**, passenger 121,363 |
| Round 5 | 98 sources / 391 routes, strict UB 14,729 | no incumbent in 600 s |

因此实际执行了多次 Search → Pool → Master 和 Master → Search 反馈，而不是一次性 master。Master 在 Round 1 和 Round 4 两次刷新 primary；island 的单体解虽未成为最终 winner，其新 route 对 Round 4 有组合价值。

## Route elimination 与结构搜索

对 89-flight incumbent 生成了逐 route audit，记录 duration、passengers、facility/service sequence、type/base、utilization、LAND flexibility、neighbor slack、overlap、geometry 与可能 target routes。Exact master 的 flight cap 找到 **14,732 / 88 flights / VALID**，证明 89 → 88 成功；没有找到 87 或更低的有效竞争解。

Targeted search 共 19 次。Cross-exchange 将 88-flight candidate 的 passenger time 从 123,171 降到 123,113，但没有 primary 收益。High-impact、facility/block、8/9-route 与 10-route neighborhoods 均未刷新，10-route 最好局部 delta 为 +28。因此 large destroy 保留为结构事件工具，不提升为普通默认参数。

## Population / recombination 判断

- A2×R3：78 个共同 physical route identities，双方各 3 个独有结构；exact difference rebuild child 为 14,770。
- Path relinking：只有 3 个差异步，0 个有效 progress，最好 14,770。
- HGS-inspired：quality-first elite population + common-route inheritance + exact recombination + ALNS education 的轻量闭环总体有效，价值主要由全 pool master 体现。
- Duplicate neighborhood memory：短期记录 route-subset fingerprint、operation 和结果，避免重复完全相同失败 neighborhood；不永久 tabu。

## 结论

Elite Route Pool 与 exact master 是本阶段最有效的新能力，带来 40 分钟 absolute improvement，并发现更少架次的 88-flight 解。最终 391 条物理 routes 已稳定，但 restricted LP gap 仍较大；当前更像 integer combination / symmetry / solver-incumbent 难题，不能据此宣称 route universe 已完备。
