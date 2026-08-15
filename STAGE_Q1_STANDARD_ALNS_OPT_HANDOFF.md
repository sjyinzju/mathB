# STAGE Q1 Standard ALNS Optimization — Handoff

**分支**：`platinumist_update_alns_base`（HEAD `2ce54cf`，worktree `C:\Users\shiju\AppData\Local\Temp\alns-audit-7d04432`）
**产出**：Strong Standard ALNS Control（ALNS-Control-V1），全部产物在 worktree `outputs/q1/alns/`，未覆盖 `outputs/q1/best`。

## 结论一句话

仅改一处——destroy 规模（stage1 2-3→5-6，stage2 3-4→6-8）——Standard ALNS 在 300s wall-clock 预算下从 15,371 稳定降至 **best 15,118 / median 15,185 / mean 15,208.4**（5/5 seeds，独立 Validator PASS），全面支配原版 control（15,276/15,354/15,331.6）。

## 阶段执行摘要

1. **Baseline 盘点（12 项）**：5 destroy 算子、两阶段 MILP repair、SA(T0=0.002, cooling=0.985)、自适应权重(reaction=0.25, segment=15)、golden 两阶段 stage 布局，全部保持不变。
2. **初解验证**：15,371 official best 经 loader 恢复 → 空跑 gate PASS（1600/1600，指标一致）。
3. **V0 multi-seed baseline（双口径）**：
   - iteration 60+60：best 15,307 / median 15,334 / beat 5/5 → 存档 `control-v0/`。
   - wallclock 300s：best 15,278 / median 15,313 / beat 5/5。
4. **关键诊断修正**：初判"SA 温度过高（worse acceptance ~92%）"是统计陷阱——逐 iteration 分解发现 551/595 accepted 为 **tie move**（repair 重建词典序等价解，deterioration=0 恒被接受），真正劣解仅 4 次。瓶颈是 destroy 规模过小，不是 SA。
5. **证据驱动消融（P1 destroy size，一次只改一项，同 seeds 0-4 对照）**：

| 配置 (s1/s2) | iteration 口径 mean | wallclock 300s mean/best |
|---|---|---|
| V0 2-3/3-4 | 15,336.2 | 15,331.6 / 15,276 |
| 3-4/3-4 | 15,315.6 | — |
| 3-4/4-5 | 15,283.8 | — |
| 4-5/5-6 (A2) | 15,154.2 (best 15,052) | 15,294.2 / 15,225 |
| **5-6/6-8 (A3=V1)** | seeds0-2: 15,131/15,093/15,196（~30min/seed，iteration 口径不经济） | **15,208.4 / 15,118** |

   tie 率 92.9%→71.8%，improving moves 39→132（A2 口径）；P3 repair fail_rate=0 无需动；P4 SA 经校准分析（T0≈0.0024 对 target 0.2）与现值 0.002 接近，跳过；P5 权重有响应（如 land_reassignment 3.75 vs random_routes 1.0），保留。
6. **Multi-start（第 10 节）**：A3 配置冷启动 300s——从 17,222 出发 → 15,753-16,093；从 15,743 出发 → 15,460-15,584；均远逊于 warm start（15,118-15,281）。**official best warm start 是正确策略**。
7. **Promotion gate**：best、median、worst、beat rate 四项 A3 全部优于 control → PASS，晋升 ALNS-Control-V1（存档 `control-v1/`，含 manifest+轨迹）。
8. **新 best 独立 Validator**：15,118（92 班次 / 1600 人 / 燃油 121,682.9 kg）→ VALID；15,278、15,215、15,052 亦逐一验证过。

## 代码与测试

- commits：`a93de02` runner → `0518aac` 诊断埋点（不触 RNG/接受规则/搜索顺序，smoke 复现 15,361 字节级确认）→ `d33fd3f` runner CLI（destroy/SA/权重参数）→ `2ce54cf` 新增 `tests/test_q1_alns.py`（3 项：初解装载、2-stage 确定性+改进、诊断字段）。
- pytest：44/44 通过。

## 20 问回答说明

任务书附件（含第 20 节 20 问原文）已随临时文件过期无法恢复，以下按已知主题覆盖；若能提供原文清单可逐条补答。

1. **V0 能否突破 15,371？** 能，双口径均 5/5（iteration best 15,307；wallclock best 15,278）。
2. **最有效算子？** V0：related_routes（improve_rate 0.148）；大 destroy 后 worst_time_per_person/low_utilization 崛起（0.287/0.237）。
3. **SA 温度问题？** 无——92% acceptance 是 tie move 假象，真正劣解接受仅 4/595。
4. **destroy size？** 唯一且充分的改进杠杆，单调收益直到 6-8；再大（iteration 口径）成本失控。
5. **repair？** 两阶段 MILP 零失败、时间充足，无需改动。
6. **自适应权重？** 有响应、保留；未做关闭消融（优先级最低，且收益证据方向明确）。
7. **fixed wall-clock 表现（Q14）？** 300s 预算下 V1 mean 15,208.4，比 iteration 口径更稳定的公平比较基准；建议后续对比统一用 wallclock。
8. **multi-start？** 冷启动显著劣于 warm start，300s 内无法追回 ~350-900 分钟差距。
9. **确定性？** 同 seed 复现验证（test + smoke）；所有新 best 过独立 Validator。
10. **STOP condition？** 已满足：control 稳定、gate 通过、配置单一变量、产物存档。下一阶段（Standard vs Relatedness-aware）应复用：同一 V1 destroy 配置、同一 wallclock 预算、同一 seed 组、同一 runner/Validator。

## 下一阶段注意事项

- Relatedness-aware 变体只能在 destroy 选路（relatedness 评分）上做文章，必须保持 V1 的 destroy 规模/repair/SA/权重配置不变才构成公平对照。
- runner 已支持全部所需 CLI 参数；`weight_history.csv`、扩展 `operator_stats.csv`（feasible/failed/gain/runtime）、convergence 结构列可直接复用做对照诊断。
- 对比时务必按 tie/improving/worsening 分解 acceptance，避免重蹈 V0 诊断覆辙。
