# FINAL INTEGRATION REPORT（2026-08-16）

# 1. Branch Status

| 分支 | HEAD | 角色 |
|---|---|---|
| `platinumist_update` | `181d0e2` | 论文基线（整合起点） |
| `codex/q1-final-or-intensification` | `be109e6` | Q1 authoritative |
| `codex/q2-round3-intensification` | `4a381e3` | Q2 authoritative |
| `codex/q3-pro-v2` | `be58e57` | Q3 authoritative（PRO V2） |
| **`final/paper-q123-integration`** | `ac86edf` | 整合分支（6 个整合 commit，见 §12） |

# 2. Final Q1 Result

- 数值：14730 min / 121363 min 在途 / 89 架次 / 118624.4 kg / 49.430% / 1600 人全覆盖。
- 算法：容量 DP + 广义节约 → 关联度引导 ALNS → 精英路线池精确主问题 → 标准 ALNS 词典序精修。
- Authoritative files：`code/outputs/q1/final/`（blob 与 q1-final 分支逐字节一致）；
  源 run `final14730-control-s21/seed-21`。
- Validator：`valid=true, issues=0`（2026-08-16 复跑）。
- 证明状态：14208.637 min 仅为不完备路线池 LP 参考界（池内间隙 3.669%），无全局最优证书。

# 3. Final Q2 Result

- 数值：17218 min / 254656 min / 95 架次 / 132473.9 kg / 92.703% / 4000 人全覆盖。
- 算法：候选路线整数主问题 + 4 路线精确局部 MILP、几何候选、精英重组、重启、
  检查点续算；第三轮普通 4 路线修复完成 4→3 吸收（96→95）。
- Authoritative files：`code/outputs/q2/best/`（源 run `20260816-q2-round3-final-repro`，
  `run_config.json` 与 repro run 字节一致）。
- Validator：`valid=true, issues=0`（2026-08-16 复跑）。
- ML 数据仅离线建模准备，未训练模型，不归因收益。

# 4. Final Q3 Result

- Stage1：28728 min（T0）/ 242455 / 162 架次 / 226835.3 kg / 57.840% / 3840 人。
- Stage2：28728 min / 251777 / 162 架次 / 226835.3 kg / 60.098% / 3997 条有效分配。
  临时人员 157/160（1.875% 未服务：P1102/P2239/P3290，CSV 字段留空）。
- Bounds：全局下界 14125 min（机场--机型分层连续多商品流，14124.895 上取整），
  UB 28728，认证区间 50.8319%；15197.6776（3116 池 LP）与 17217.4167
  （定价缩减池受限主问题，229 变体/107 列）均为池内参考界。
- 157 为固定 162 架次结构下的可证明最优；unrestricted 158/159/160 仍 open。
- 演进链：30546（v6-deep）→ 29659（Closure--P2）→ 29155（PRO V1）→
  28868（V2 筛选+四岛深度 ALNS）→ 28728（Rescue+P0 反馈）；跨日结构搜索贡献
  深度阶段 23/23 次全局最好（766 尝试/29 接受）。
- Authoritative files：`code/outputs/q3/q3-pro-v2/current_incumbent/`
  （blob 与 q3-pro-v2 分支逐字节一致；formal run `q3-pro-v2-deep-v1`，9763.63 s）。
- Validator：两阶段均 `valid=true, issues=0`（2026-08-16 复跑）。

# 5. Paper Changes

基线 2709 行 → 约 2790 行；Q1/Q2 成熟章节零改动。逐条清单见
`docs/paper_update_audit.md`，要点：

- 摘要 Q3 段改为 V2 二阶段结果（28728/162/157/50.832%）。
- 新增 PRO V1/V2 小节 + `tab:q3-operator-v2`（766/29/23，跨日 128/28/23）。
- 界限节加定价缩减池参考界与 bounds 对照表；UB=28728、gap=50.832%。
- 新增 `tab:q3-evolution` 演进链表；结果/消融/比较表全部换为 V2 数值。
- 局限与结论同步（fixed-structure 表述、158/159/160 open）。
- 附录清单与代码清单加入 q3-pro-v2 产物、`q3_pro.py`、`q3_pro_v2.py`、12/13/14 脚本。

# 6. Algorithm Changes

本次整合**不引入任何新算法**：Q3 从 29659 提升到 28728 的全部工作
（PRO V1 精英池邻域、PRO V2 异构筛选/四岛深度 ALNS/Optional Rescue/强制 P0 反馈）
均原样取自 `codex/q3-pro-v2` 分支的已验证代码与 run 产物；整合仅做代码/产物/论文
的同步与测试修复。三类精确邻域（guided LNS、local branching、aircraft-day 窗口）
零改善的负结果如实写入论文。

# 7. Figures

- 重生成（基于 28728 Stage2 CSV，脚本输出确认 flights=162/served=3997）：
  `q3_gantt`、`q3_aircraft_utilization`、`q3_time_window_margin`
  （`code/scripts/plot_q3_results.py`）。
- 新增：`q3_search_evolution`（`code/scripts/plot_q3_search_evolution.py`，
  五个数值来源见脚本 docstring）。
- 沿用未变：Q1 收敛图、Q2 结果图、数据质量图（输入 CSV 未变）。

# 8. Tables

新增 `tab:q3-evolution`、`tab:q3-operator-v2`；更新 `tab:q3-results`、
`tab:q3-stage1-comparison`、`tab:q3-ablation`（10 行）、bounds 对照表（4 行）。
Q1/Q2 各表未动。

# 9. Repository Reorganization

```
main.tex / figures/ / references.bib       论文（Q3 已升级，已重编译）
code/src|scripts|tests|validation|configs  三问求解与测试（96 项）
code/outputs/q1/final/                     Q1 最终产物
code/outputs/q2/best/                      Q2 最终产物
code/outputs/q3/q3-pro-v2/current_incumbent/  Q3 最终产物（正式提交源）
code/outputs/q3/{best,closure_p2_best,q3-pro}/  Q3 历史阶段产物（29155/29659/PRO V1 runs）
submission/                                六份官方格式提交 CSV + 打包说明
docs/                                      result_provenance / reproduction / paper_update_audit
README.md                                  入口文档
```

根目录原有 handoff/报告文件全部保留，未删除任何无法确认用途的文件。
`.gitattributes` 强制 csv/json/jsonl/md/py 以 LF 存储（修 autocrlf 字节比对测试）。

# 10. Validation

- `python -m pytest`：**96 passed**（72.3 s，2026-08-16 复跑）。
- 四套 Validator 重跑：Q1 / Q2 / Q3-Stage1 / Q3-Stage2 全部 `valid=true, issues=0`。
- LaTeX：xelatex + bibtex 全量编译成功（MiKTeX 25.12，本次整合中新装）；
  `main.log` 无 undefined references/citations；63 个 `\inputcode` 与 8 张图全部存在；
  全文 784 页（附录含全部源码）。
- 已知合规事项：正文+参考文献 42 页 > 官方 30 页限制（整合前旧版已 40 页，
  既有问题），经队伍决定暂不压缩、后续处理。
- latexmk 需 perl（MiKTeX 25.12 不再附带），本次用等价手工管线编译，
  复现方式已写入 `docs/reproduction.md`。

# 11. Remaining Issues

1. **Q1 strict proof**：仍无原问题全局最优证书（路线池不完备）。
2. **正文 42 页超限**：需队伍人工压缩至 ≤30 页（已获准暂缓）。
3. Q3 unrestricted 158/159/160 可行性 open；认证区间 50.832% 仍宽。
4. 支撑材料打包需核对 ≤20 MB（`code/outputs/q3/q3-pro-v2/` 约 13 MB）。
5. 论文 PDF 与压缩包需按三位队号命名后提交（本仓库不含身份信息，命名在提交时完成）。

# 12. Git Status

- 整合分支：`final/paper-q123-integration`（本地，未 push）。
- 基线 `181d0e2` 之上的 6 个整合 commit：
  - `864589d` pull Q3 PRO V2 code and authoritative outputs
  - `9f85131` promote q3 best/ to PRO V1 29155; point pytest at code/ tree
  - `a8977cb` sync Q1/Q2 authoritative best artifacts, consensus data, LF gitattributes
  - `17bbcf9` align q2 best run_config with immutable repro run
  - `f1394e6` paper: upgrade Q3 to PRO V2 final (28728/162/157)
  - `ac86edf` docs: README, provenance, reproduction, paper audit, submission CSVs
- 全程无 force push、无历史改写；main 未改动；四个 source 分支只读。
