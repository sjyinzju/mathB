# B 题 FINAL INTEGRATION 实施计划

> **For agentic workers:** 本计划在本次会话内以 Inline 方式执行（任务书明确要求审计后直接完成整合，不再等待确认）。执行技能：superpowers:executing-plans 的批量执行+检查点原则。

**Goal:** 以四分支真实 validated outputs 为唯一依据，完成论文 Q3 升级、代码/结果整合、图片重生成、仓库整理、README/provenance/reproduction、submission 包与 integration branch。

**Architecture:** 以 `origin/platinumist_update`（论文分支，已含 Q1=14730/Q2=17218 最新内容与 `code/` 统一代码树）为整合基座，从 `origin/codex/q3-pro-v2` 复制 Q3 PRO V2 代码（`q3_pro.py`/`q3_pro_v2.py`/`q3_timing.py` 等）、脚本（`12_run_q3_pro.py`/`13_run_q3_pro_v2.py`/`14_run_q3_pro_v2_robustness.py`）、测试与全部 authoritative 产物到 `code/outputs/q3/q3-pro-v2/`；重写 main.tex 的 Q3 章节、摘要、界限、局限与结论；用最终 28728 CSV 重生成全部 Q3 图；重跑 pytest 与独立 Validator；latexmk 全量编译。

**Tech Stack:** Python 3 + scipy/HiGHS MILP、matplotlib（图）、pytest、xelatex/latexmk、Git worktree。

---

## Phase 1 审计结论（已完成）

### 分支状态（均已 fetch，本地与 origin 同步）
| 分支 | HEAD | 角色 |
|---|---|---|
| `platinumist_update` | 181d0e2 | 论文+工程基座（Q1/Q2 已最新，Q3 停留在 29659） |
| `codex/q1-final-or-intensification` | be109e6 | Q1 authoritative：`outputs/q1/final/` |
| `codex/q2-round3-intensification` | 4a381e3 | Q2 authoritative：`outputs/q2/best`（repro `20260816-q2-round3-final-repro`） |
| `codex/q3-pro-v2` | be58e57 | Q3 authoritative：`code/outputs/q3/q3-pro-v2/current_incumbent/` |

### Authoritative finals（metrics.json × validator.json 交叉验证通过）
- **Q1**: 14730 / 121363 / 89 / 118624.4 / 49.4297% / 1600，0 issues；源 run `final14730-control-s21/seed-21`，SHA-256 已记录。
- **Q2**: 17218 / 254656 / 95 / 132473.9 / 92.7026% / 4000，0 issues；96→95 来自普通 4-route exact repair；ML 不归因。
- **Q3 Stage1**: 28728 / 242455 / 162 / 226835.3 / 57.8404% / 3840，0 issues。
- **Q3 Stage2**: 28728 / 251777 / 162 / 226835.3 / 60.0985% / 3997（157/160），未服务 P1102/P2239/P3290。
- **Bounds**: 全局 LB 14125（多商品流，14124.895 上取整）；UB 28728；认证 gap 50.8319%；restricted-pool LP 17217.4167、finite-pool LP 15197.6776 均仅池内参考；Stage2 157 为 fixed-structure 最优，158/159/160 unrestricted 仍 open。
- **Q3 演进**: 30546 → 29659（Closure-P2）→ 29155（PRO V1）→ 28868（V2 screen/deep，Cross-Day 主导：766 attempts / 29 accepted / 23 global-best，其中 Cross-Day 28/23）→ 28728（Optional Rescue + mandatory P0 feedback，−140 min）。
- **Screen**: 20 configs × 2 seeds：22@29155、13@29006、5@28868。Deep：4 islands × ≤500 iter。Exact neighborhoods（guided LNS 50 窗、local branching r=5..80、aircraft-day chain 10 窗）均 0 改善。Pricing：363 routes 入池；12 elites、3116 route variants、179 flight columns。
- **官方要求**: 摘要 1 页、正文 ≤30 页、附录不限；附录含支撑材料清单+全部可运行源码+AI 声明；论文单文件 PDF ≤20MB；支撑材料 RAR/ZIP ≤20MB；结果 CSV 按题目附录模板。

### 论文（platinumist_update main.tex，2709 行）
- Q1/Q2 章节、图表、界限措辞已最新（14730/17218，restricted 界限定正确）→ 保留。
- Q3 需整段升级：摘要 L97-98、L2243-2263（P2 叙述）、L2370-2391（bounds 表与 gap）、L2396-2420（流程步骤）、L2422-2552（结果表/消融/图注）、L2564（局限）、L2570（结论）、附录 L2592-2693（文件清单+代码清单）。
- Q3 图（q3_gantt、q3_aircraft_utilization、q3_time_window_margin、q3_daily_pressure、q3_slack_by_task）基于旧 29659 解 → 全部用 28728 Stage-2 CSV 重生成。

---

## Phase 2 执行任务

### Task 1: 建立整合 worktree 与分支
- 从 `origin/platinumist_update` 建分支 `final/paper-q123-integration`，worktree 放在 `%TEMP%\mathB-final`（不污染主目录）。
- 不 force push、不动 main。

### Task 2: 整合 Q3 PRO V2 代码与产物（从 q3-pro-v2 分支复制）
- `code/src/solver/`：`q3_pro.py`、`q3_pro_v2.py`；核对 `q3_timing.py`/`q3_bounds.py`/`q3_closure_p2.py`/`q3.py`/`importer.py` 差异，按 q3-pro-v2 版覆盖（它是超集基座）——仅覆盖 Q3 相关文件，不动 Q1/Q2。
- `code/scripts/`：`12_run_q3_pro.py`、`13_run_q3_pro_v2.py`、`14_run_q3_pro_v2_robustness.py`。
- `code/tests/`：`test_q3_pro.py`、`test_q3_pro_v2.py`。
- `code/outputs/q3/q3-pro-v2/`：current_incumbent（Stage1+Stage2 CSV/validator/metrics/bounds/run_config）、reports、parameter-screen.csv、deep-islands.csv、convergence.csv、bottleneck-report.json、optional-rescue-dossier-v2.json、final-feedback.json、secondary-polish.json、robustness-v2.json、elite_pool、route_library、column_library（跳过超大 checkpoints/runs 中与论文无关的中间件，保留报告引用的部分）。
- 保留旧 `code/outputs/q3/best`、`closure_p2_best`、`p0_p1_best` 作为演进证据（论文算法演化表引用）。

### Task 3: 重生成 Q3 图（基于 28728 Stage-2 CSV）
- 用整合仓库 `code/scripts/plot_q3_results.py --routes current_incumbent/q3-routes.csv --assignments q3-assignments.csv` 重生成 gantt / utilization / time-window-margin / daily-pressure / slack-by-task 到 `figures/`（PNG+PDF）。
- 新增 `figures/q3_search_evolution.{png,pdf}`：30546→29659→29155→28868→28728 演进图（小脚本，数据来源 metrics/报告）。
- 重新统计图注用数值（最忙飞机/日期、裕度中位数等）并记录，供论文正文更新。

### Task 4: main.tex Q3 全面升级（保留成熟 Q1/Q2 文本）
- 摘要：Q3 段改为 V2 二阶段结果（28728、162 架次、157/160、gap 50.83%）。
- Q3 章节：新增/改写小节——异构参数筛选（40 runs）、Multi-Island Deep ALNS（4 islands）、Cross-Day 核心发现（算子统计表）、Optional Rescue → Mandatory P0 Feedback（28868→28728）、exact neighborhoods 负结果一段、pricing/结构资产一小段。
- 结果表：Stage1/Stage2 更新为 28728/162/157；新增算法演化表（30546→29659→29155→28868→28728）与 screen/islands 摘要表、Cross-Day 算子贡献表、bounds scope 表（全局 LB 14125 / restricted-pool 17217.4167 / finite-pool 15197.6776，明确证明范围）、三问总结果表。
- 措辞红线：fixed-structure optimum ≠ 全局；finite-pool LP ≠ global bound；不宣称 global optimality；157 不比旧 158 退步（lexicographic 解释）。
- 局限与结论同步更新；Q1 optimality 保留"高质量可行上界+有限池精确重组，严格认证另行进行"模块化表述。
- 附录：支撑材料清单加入 q3-pro-v2 目录；代码清单加入 `q3_pro.py`、`q3_pro_v2.py`、12/13/14 脚本。
- 伪代码：保持 ≤4 个 Algorithm block（若现有已满足则不新增）。

### Task 5: 验证（pytest + Validator 重跑 + LaTeX）
- `pytest`（整合仓库 code/tests）；历史已知非 Q3 failures（q1-relatedness-consensus 缺失、Q1 期望值、Q2 CRLF/atomic）逐项核实：过时的更新到 authoritative 行为并说明，不静默删除。
- 独立 Validator 对四套最终 CSV 全部重跑并保存 validator.json（q1、q2、q3-base、q3）。
- `latexmk -xelatex -bibtex main.tex` 全量编译，检查 undefined refs / 缺图 / 页数（正文≤30 页）。

### Task 6: 仓库整理与文档
- `README.md`（根）：Problem Summary、Final Results（自动取自 metrics）、Method Overview、Repository Structure、Reproduction（full + validate-existing 两条路径）、Validation、Paper 编译、Submission 位置、Provenance、Q1 proof status。
- `docs/result_provenance.md`：Q1/Q2/Q3(Stage1/Stage2) 的 source branch、run、文件、SHA、validator 摘要。
- `docs/reproduction.md`：Python 版本、依赖、种子、三问命令、validator、图生成、论文编译。
- `docs/paper_update_audit.md`：本次审计与更新记录（按章节）。
- `submission/`：仅 6 个正式 CSV + 最终论文 PDF 占位说明（PDF 编译后放入）。
- 不删除任何无法确认用途的文件；旧实验保留原位。

### Task 7: 提交与报告
- 在 `final/paper-q123-integration` 分支提交；不 push 到 main、不 force。
- 输出 FINAL INTEGRATION REPORT（任务书第 56 节的 12 项）。

## 风险与红线
- 任何论文数字必须来自 metrics.json / validator.json / CSV 复算；不手工对齐。
- 若最终 CSV 未过 Validator → 暂停并报告（目前审计全部通过）。
- 不修改 Q1/Q2 成熟文本，除数字核对发现的错误外。
