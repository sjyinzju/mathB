# Paper Update Audit（2026-08-16 最终整合）

记录本次整合对 `main.tex` 的全部修改，确保论文数字与仓库产物一一对应。
基线：`platinumist_update @ 181d0e2`（2709 行）；整合后约 2790 行。

## 修改原则

- 全部数字取自 `code/outputs/**/metrics.json`、`bounds.json` 与 CSV 复算，禁止手改。
- restricted LP 一律标注“池内参考界”，不写成 global lower bound。
- Stage2 157 仅表述为 fixed-structure optimum；158/159/160 unrestricted 为 open。
- Q1/Q2 成熟章节（14730/17218 及相关措辞）保持原样，未改动。

## 修改清单（Q3 升级）

| 位置 | 修改内容 |
|---|---|
| 摘要 Q3 段 | 改为 V2 二阶段结果：28728/162 架次/157 人/gap 50.832%；加入异构筛选、4 岛、Cross-Day 主导、fixed-structure 表述 |
| `\subsubsection{PRO V1精英池邻域与PRO V2深度强化}` | 新增：40 screen runs 三档收敛、四岛均 28868、Rescue+P0 反馈 −140、`tab:q3-operator-v2`（766/29/23，跨日 128/28/23）、三类精确邻域负结果、定价与资产（363 路线入池、12 精英 0.0519、3116 变体、179 列） |
| 求解界限节 | 新增定价缩减池受限主问题 17217.417（229 变体/107 列）；bounds 对照表 4 行；UB=28728、gap=50.832%；Stage2 词典序不退步论证 |
| 流程 enumerate | 步骤扩为含 PRO V1（29155）、PRO V2（28868）、Rescue+P0（28728）、162 架次/3 名空分配 |
| `tab:q3-stage1-comparison` | 增加 PRO V1（29155/241018/165/231384.5）与最终（28728/242455/162/226835.3）两行 |
| `tab:q3-evolution` | 新增演进链表：29659/168 → 29155/165 → 28868/163 → 28728/162 |
| `tab:q3-results` | Stage1 28728/242455/162/226835.3/57.840；Stage2 157/28728/251777/162/60.098 |
| margin 图注 | n=3997、median=1437、应急 median=19、增储 54.5（基于 Stage2 CSV 复算） |
| `tab:q3-ablation` | 扩为 10 行（加 Closure--P2 反馈 29659/158、PRO V1 29155/157、PRO V2 28868/157、最终 28728/157） |
| 稳定性段 | 28728 保护性候选措辞更新 |
| 支撑材料段 | 3997 条有效/3 条留空/两阶段 Validator 零违规 |
| `fig:q3-search-evolution` | 新增演进图（30546→29659→29155→28868→28728），脚本 `code/scripts/plot_q3_search_evolution.py` |
| 模型局限段 | 28728/50.832%、双池参考界、157 固定 162 架次、差 3 人且 158/159/160 open |
| 结论段 | Q3 句改为 V2 全链叙述（28728/162/157/50.832%/差 3 人） |
| 附录支撑材料清单 | 加 `q3-pro/`、`q3-pro-v2/current_incumbent/`、`q3-pro-v2/reports/`、`q3_pro.py`、`q3_pro_v2.py`、`12–14` 脚本；`q3/best/` 描述改为 PRO V1 产物 |
| 附录代码清单 | 加 `q3_pro.py`、`q3_pro_v2.py`、`12_run_q3_pro.py`、`13_run_q3_pro_v2.py`、`14_run_q3_pro_v2_robustness.py`、`plot_q3_search_evolution.py` |
| 补充结果节 | 正式提交指向 `q3-pro-v2/current_incumbent/` |

## 图片重生成（全部基于 28728 Stage2 CSV）

- `figures/q3_gantt.{pdf,png}`、`q3_aircraft_utilization.{pdf,png}`、
  `q3_time_window_margin.{pdf,png}`：`code/scripts/plot_q3_results.py`
  （脚本输出确认 flights=162、served=3997）。
- `figures/q3_search_evolution.{pdf,png}`：`code/scripts/plot_q3_search_evolution.py`（新增）。
- 其余沿用图（Q1/Q2/数据质量）与 CSV 未变，未重绘。

## 编译核验（2026-08-16）

- xelatex+bibtex 三遍编译成功；`main.log` 无 undefined references/citations。
- 63 个 `\inputcode` 文件、8 张图全部存在；`\ref/\eqref` 与 `\label` 零缺失。
- 全文 784 页（附录含全部源码）。正文+参考文献 42 页，超出官方 30 页限制；
  旧版即为 40 页（既有问题），经队伍决定暂不压缩，后续处理。
