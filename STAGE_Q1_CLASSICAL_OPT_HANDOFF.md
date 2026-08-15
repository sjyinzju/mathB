# Q1 Classical Optimization 阶段交接

## 结果与阶段门

本阶段从确定性 B1 Generalized Savings 开始，在不使用聚类、Bandit 或机器学习的前提下，加入批量 relocation、LAND 路线 ejection/repacking 和 VND 式重复收敛检查。所有 best 均重新导出官方 CSV，并由独立 Validator 复算。

| 版本 | 飞机时间/min | 乘客时间/min | 架次 | 燃油/kg | 座位利用率 |
|---|---:|---:|---:|---:|---:|
| B0 Safe Baseline | 17,222 | 120,609 | 110 | 142,601.0 | 0.430474 |
| B1 Savings（阶段起点） | 15,743 | 120,396 | 100 | 128,562.3 | 0.467567 |
| Classical VND best | **15,371** | 120,870 | **95** | **123,081.7** | **0.482255** |

当前 best 相比 B1 再降低 372 分钟（2.36%），相比 B0 降低 1,851 分钟（10.75%）。服务人数 1600/1600，Validator PASS，内部指标与 Validator 一致。最佳文件位于 `outputs/q1/best/`。

## 实际采用的算子

- `generalized_savings_merge`：同机场整路线合并；完整枚举机型、服务顺序、技术经停和加油。B1 接受 10 次，降低 1,479 分钟。
- `batch_relocation_rebuild`：把同一 OD/起点类型的人员批次在两条路线间迁移；LAND 可跨机场，固定机场人员只能留在其机场；两条路线均重新优化机型、顺序、技术经停和加油。最终轨迹累计贡献 71 分钟。
- `land_route_ejection_chain`：把一条全 LAND 单设施路线原子拆入两条有余量路线，一次性删除源架次，允许跨机场。最终轨迹删除 5 条路线，累计贡献 301 分钟。
- 接受规则始终使用严格词典序，飞机时间第一；候选先用直达时间下界剪枝，最终必须经过精确 evaluator。

## 有效与无效尝试

- 有效：从“仅最近路线”扩展为“近邻 + 高余量”候选池，又找到一次 F035 的 14 人 ejection，减少 23 分钟和 1 架次。
- 有效：ejection 后再次运行 relocation，先后再降低 3 和 28 分钟，说明两个邻域需要交替收敛。
- 无效：单独把 F036 的 11 人跨机场迁移是中性步骤，严格单步 relocation 不接受；必须将 15 人同时拆入两条路线，原子删除源架次，才能降低 129 分钟。
- 无效：最终 best 上再次运行 Savings 和双目标 ejection 均为 0 个可接受动作；relocation 已运行到本邻域无进一步改进。
- 未采用：clustering、监督学习、Bandit、ALNS 和随机多起点。本阶段保持纯 classical control。

## 性能、接口与测试

- B0 约 33 秒；B0→B1 约 66 秒；完整 relocation→ejection 轨迹约 149 秒。
- relocation 是当前瓶颈：一次后期实验检查 2,943 个 move、下界剪枝 1,200 个、精确处理 1,743 个，触发 5,417 次路线评价，仅换取 28 分钟改进。
- 主要重复成本来自 technical-stop search、机型枚举和受影响路线重构；当前缓存只在单个算子调用内共享，尚未跨阶段持久复用。
- 新增 `load_q1_solution(...)`，可从 `outputs/q1/best/` 恢复内存 Solution，后续实验无需重跑 B0/B1；恢复再导出与 best 的 routes/assignments 字节一致。
- 自动化测试 32 项全部通过，覆盖 Savings、批量 relocation、跨中性状态 ejection、官方 CSV 往返和当前 best 回归门。

关键入口：

```powershell
python -m pytest -q
python scripts/03_solve_q1_baseline.py --start-best --relocate --promote
python scripts/03_solve_q1_baseline.py --start-best --ejection --promote
python scripts/validate_solution.py --question q1 --routes outputs/q1/best/q1-routes.csv --assignments outputs/q1/best/q1-assignments.csv
```

## 当前瓶颈与下一步

当前解在 whole-route merge、双路线 batch relocation 和两目标 route ejection 三个确定性邻域下已稳定。剩余突破口需要更大的结构变化，例如三目标 ejection、route split/recombination 或传统 ALNS destroy/repair；但预计计算成本会显著增加。

Stage 1 的 `closed_route_reachability`、`refuel_hub_summary` 和 `leg_features` 目前主要用于离线审计，尚未系统进入候选排序。后续最值得先做的是共享 technical-stop/route cache，并把候选排序抽象成可注入接口。

该接口正适合第一次接入 clustering 线程：只改变 relocation/ejection/Savings 的候选先后顺序，不做硬分区、不剪掉跨簇候选，并与本阶段 15,371 分钟 classical best 使用相同评价预算和 Validator 做公平对照。若比赛时间允许，可另行做小预算传统 ALNS 或多起点作为补充；当前 classical best 已足以冻结为融合实验基准。
