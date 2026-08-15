# Stage: Shared Solver Performance Infrastructure — Handoff

分支 `performance/shared-infrastructure`（commits `bfbc8b4`、`d28df2d`、`af12587`）。
目标：在不改变任何算法语义的前提下，消除跨算子/跨阶段的重复物理计算，为 ALNS、clustering、Q2/Q3 提供共享高性能底层。

## 1. 优化前的真实瓶颈

cProfile 显示 relocation 场景 99% 时间（144.4s / 145.8s）在 `augment_service_sequence`（技术经停 Pareto 标签搜索）：2470 万次 label 构造/支配检查、2620 万次 `flight_minutes`、3410 万次 `fuel_for_leg`。已有的去重只在单算子内生效（augmentation calls==unique），Savings→relocation→ejection 之间全部重复计算。pandas 不在热路径上，未动。

## 2. 实际采用的架构

- **`src/solver/physics.py` — `LegPhysics`**：求解启动时一次性预计算 3 机型 × 55 × 55 = 9075 条 (distance, ceil flight minutes, fuel burn)。直接用 Stage 1 `rules.flight_minutes` / `rules.fuel_for_leg` 公式构建，测试穷举证明与公式 bit-identical。O(1) 查表，single source of truth。
- **`src/solver/cache.py` — `SolverCache`**：run-scoped 共享缓存对象（无全局单例，每个 run/seed 一个实例即可隔离），含 4 层 dict + 8 组 hits/misses 计数器，`stats()` 写入 run_config["performance"]。
- **`src/solver/technical_stops.py` 内层提速（语义严格不变）**：Pareto label 从 frozen dataclass 改为纯 tuple、支配检查内联（消除 2470 万次调用帧）、`physics.table_for(type)` 单次 dict 查表替代两次嵌套 dict + 两次函数调用、预计算后缀服务集合与每节点 (refuel, dwell) 选项。支配规则、path_key 裁决、候选顺序全部原样保留。
- **集成**：`scripts/03_solve_q1_baseline.py` 创建一个 `SolverCache` 贯穿 baseline/Savings/relocation/ejection；`improve.py` / `baseline.py` 全部算子接受 `cache=None`（None 时本地新建，向后兼容）。

## 3. 缓存了什么 / Cache key / 生命周期

| 层 | Key | 内容 | 生命周期 |
|---|---|---|---|
| augmentation | `(base_airport, aircraft_type, ordered_service_nodes)` | 完整技术经停搜索结果（stops、refuel 决策、静态时间/燃油） | run 内跨阶段 |
| skeleton | `(secondary_order, base, od_count_signature)` | 最优 (机型, stops, service_order) 静态骨架 | run 内跨阶段 |
| lower_bound / direct_time | 同上签名 | 静态时间下界 / 直飞时间 | run 内跨阶段 |

augmentation key 安全的原因：`augment_service_sequence` 从不接触乘客/载荷，(base, 机型, 有序服务节点) 完全决定结果（stop_limit / candidate_nodes 当前所有调用点均为默认值；若未来变化必须扩展 key）。skeleton key 中 `od_count_signature` = 排序后的 (origin, dest, count) 多重集，精确编码载荷轮廓。未做磁盘缓存——问题规模下内存 dict 即够，条目上限千级。

## 4. 明确禁止缓存的量

- 完整 `RouteEvaluation`（乘客旅行时间、容量/载荷可行性）——skeleton 命中后仍对实际 assignments 重算 `evaluate_route`；
- 任何 assignment-dependent 的动态评价；
- 不缓存"相同 facility pair → 相同 route move"这类跨上下文的 candidate 结论。

## 5. 关键修改文件

`src/solver/physics.py`（新）、`src/solver/cache.py`（新）、`src/solver/technical_stops.py`、`src/solver/improve.py`、`src/solver/baseline.py`、`src/solver/__init__.py`、`scripts/03_solve_q1_baseline.py`、`tests/test_performance_infra.py`（新，9 项）。

## 6. Before / After（同一机器、同一输入、同一 seed）

| 场景 | Before | After | 提速 |
|---|---|---|---|
| B0 + Savings（字节对照 b1-final） | 67.4s | 26.5s | 2.5× |
| b10 → relocate(targets=2, iter=8)（字节对照 b11） | 57.9s | 22.3s | 2.6× |

函数调用总量 375M → 68M。两条黄金流程的 q1-routes.csv / q1-assignments.csv 均**字节一致**（15,743 → 15,371 全程不变）。

## 7. Cache hit 统计（实测）

- b0+savings：augmentation 1816 hits / 878 misses（67%）；direct_time 82%。
- relocate：augmentation 2051 / 685（75%）；skeleton 2693 / 793（77%）；lower_bound 4715 / 1171（80%）；direct_time 3445 / 869（80%）；entries 分别 685 / 793 / 1171 / 869，内存开销可忽略。

## 8. Tests

`pytest` 41 项全过（32 原有 + 9 新增）：LegPhysics 穷举等于 rules 公式；缓存==未缓存；key 不跨机型/基地机场/服务顺序共享；technical-stop route 与 refuel witness 经缓存后完全恢复；30 组随机差分（含 technical-stop case）；载荷变化不误命中（skeleton 命中仍重算乘客评价）；共享/清空缓存下确定性搜索输出不变；stats 键完整且 `leg_physics_entries == 9075`。

## 9. Q1 回归结果

B0 复现与存档 b0-final-code 字节一致（17,222）；b11 复现字节一致（15,371）；官方 Validator 对 best 输出 VALID；best reload + relocate 收敛验证 accepted_moves=0、gate PASS；metrics 五项全部对齐（15,371 / 120,870 / 95 / 123,081.7 / 0.482255）。

## 10. Q2 / Q3 复用

共享层只描述 aircraft/route physics（距离、飞行时间、燃油、reserve、技术经停、静态航路时长），不含任何 Q1 单向载客假设。Q2 pickup-delivery 与 Q3 candidate sortie / scheduling 直接复用 `LegPhysics` 与 `SolverCache.augmentation_result`；乘客/时间窗等动态层留在各自问题上层。

## 11. ALNS / clustering 分支接入

merge 或 rebase 本分支三个 commit；operator 内把"调用 `augment_service_sequence(...)`"替换为"接受外部传入的 `SolverCache` 并调用 `cache.augmentation_result(...)`"即可。cache 不改变任何 candidate ordering、接受规则、operator selection，A/B 对照保持受控。

## 12. 当前风险

- augmentation key 未编码 stop_limit / candidate_nodes（当前全部默认值）；未来若有调用点传非默认值，必须先扩展 key。
- skeleton 命中仅省静态搜索，`evaluate_route` 仍按实际载荷重算——这是正确性要求，剩余热点也在此处。
- 提速为纯 Python 层 ~2.5×；若 ALNS 大规模评价仍不足，下一步是 evaluator 向量化/C 扩展，而非继续加缓存。
