# 2026 年度“策联杯”数学建模精英联赛 B 题：海上油田人员直升机运载计划编排

本仓库为最终整合版（分支 `final/paper-q123-integration`），包含论文（`main.tex`）、
三问全部求解代码（`code/`）、已验证结果产物（`code/outputs/`）与提交文件（`submission/`）。

## Problem Summary

海上油田拥有 3 座陆地机场、52 座海上设施和 3 种直升机。三个问题依次为：

- **Q1**：1600 人单向出海运输（容量、油量、最多 5 次海上着陆、技术经停）。
- **Q2**：4000 人出海、海返与设施间穿梭联合运输（同架次先下后上、禁止换乘）。
- **Q3**：4000 人 7 天多日排班（时间窗、24 架固定属地飞机、06:00–18:00 起飞、
  20:00 前返回、30 min 周转；第一阶段完成 3840 名必运人员，第二阶段在
  $T_{\mathrm{air}}\le T_0$ 下尽量服务 160 名临时人员）。

首要目标均为总飞机使用时间，按词典序依次优化人员总在途时间、总架次数、总燃油消耗，
座位利用率作为并列时的评价量。独立 Validator 从最终 CSV 复算全部硬约束。

## Final Results（均取自 `code/outputs/**/metrics.json` 并经 Validator 复验）

| 问题 | 飞机时间/min | 在途时间/min | 架次 | 燃油/kg | 座位利用率 | 人数 | Validator |
|---|---|---|---|---|---|---|---|
| Q1 | 14730 | 121363 | 89 | 118624.4 | 49.430% | 1600/1600 | 0 issues |
| Q2 | 17218 | 254656 | 95 | 132473.9 | 92.703% | 4000/4000 | 0 issues |
| Q3 Stage1 | 28728 | 242455 | 162 | 226835.3 | 57.840% | 3840/3840 | 0 issues |
| Q3 Stage2 | 28728 | 251777 | 162 | 226835.3 | 60.098% | 3997（临时 157/160） | 0 issues |

**Bounds（证明范围严格限定）**：Q3 全局下界 14125 min（机场--机型分层连续多商品流），
认证区间 50.832%；15197.6776 与 17217.4167 仅为候选池内参考界；
Stage2 的 157 人为固定 162 架次结构下的可证明最优，不限结构的 158/159/160 仍未关闭。
Q1/Q2 的求解界同样仅属于有限候选主问题或当次局部邻域，不构成原问题全局最优性证明。

Q3 算法演进链：30546（旧深度解）→ 29659（Closure--P2）→ 29155（PRO V1）→
28868（V2 异构筛选 + 多岛深度 ALNS）→ 28728（Optional Rescue + 强制 P0 反馈）。

## Method Overview

- **Q1**：单设施容量 DP + 广义节约初解 → 关联度引导 ALNS → 精英路线池精确主问题 → 标准 ALNS 词典序精修。
- **Q2**：候选路线整数主问题 → 4 路线精确局部 MILP 修复、几何排序有界候选、
  质量精英重组、全局重启、检查点续算、低频 5 路线强化；第三轮完成 4→3 吸收（96→95 架次）。
- **Q3**：燃油可行路线复用 + 时间窗拼载 + 具体飞机区间日历排班 + 稀疏 0--1 重分配；
  P0 两阶段投影反馈闭环、Closure--P2 结构重构、PRO V1 精英池邻域、
  PRO V2 异构参数筛选/四岛深度 ALNS/Optional Rescue 与强制 P0 反馈。

## Repository Structure

```
main.tex / figures/ / references.bib   论文与图（图均由 figures/*.csv 可复现生成）
code/src/solver/                       三问求解核心（q1_or、q2*、q3*、q3_pro*）
code/src/validation/                   独立 Validator
code/scripts/                          运行入口与绘图脚本
code/tests/                            pytest 测试（96 项）
code/outputs/q1/final/                 Q1 最终产物
code/outputs/q2/best/                  Q2 最终产物
code/outputs/q3/q3-pro-v2/current_incumbent/   Q3 最终产物（两阶段 CSV/metrics/bounds/validator）
code/outputs/q3/best/                  Q3 PRO V1 阶段产物（29155）
code/outputs/q3/closure_p2_best/       Closure--P2 阶段产物（29659）
submission/                            正式提交 CSV（六份）
docs/                                  provenance、复现与论文更新审计
```

根目录保留各阶段 handoff/报告文档（`STAGE_*.md`、`Q*_*.md` 等），未删除。

## Reproduction

两条路线（详见 `docs/reproduction.md`）：

1. **validate-existing（秒级）**：直接对仓库内已固化 CSV 重跑 Validator 与 pytest。
2. **full（小时级）**：按 `docs/reproduction.md` 的命令逐步重跑三问求解与图生成。

## Validation

```powershell
cd code
python -m pytest                                   # 96 passed
python scripts\validate_solution.py --question q1 --routes outputs\q1\final\q1-routes.csv --assignments outputs\q1\final\q1-assignments.csv
python scripts\validate_solution.py --question q2 --routes outputs\q2\best\q2-routes.csv --assignments outputs\q2\best\q2-assignments.csv
python scripts\validate_solution.py --question q3 --routes outputs\q3\q3-pro-v2\current_incumbent\q3-base-routes.csv --assignments outputs\q3\q3-pro-v2\current_incumbent\q3-base-assignments.csv
python scripts\validate_solution.py --question q3 --routes outputs\q3\q3-pro-v2\current_incumbent\q3-routes.csv --assignments outputs\q3\q3-pro-v2\current_incumbent\q3-assignments.csv
```

四套输出均为 `valid=true, issues=0`（2026-08-16 整合后复跑确认）。

## Paper

`xelatex + bibtex`（或 `latexmk -xelatex -bibtex`）编译 `main.tex`。
附录含全部源码（约 780 页）。已知事项：正文+参考文献合计 42 页，超出官方 30 页
限制，为整合前既有问题，由队伍后续压缩。

## Provenance

各问最终解的来源分支、run、文件 SHA-256 与校验摘要见 `docs/result_provenance.md`。
Q1 证明状态：无原问题全局最优证书；14208.637 为不完备路线池 LP 参考界。
