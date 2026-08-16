# Root Cleanup Audit（2026-08-16，分支 chore/repository-cleanup）

基线：main @ `c81c2d8`；基线 pytest **96 passed**；submission 六 CSV 与 HEAD blob 逐字节一致。
原则：UNKNOWN→KEEP，IMPORTANT→KEEP，HISTORICAL BUT USEFUL→ARCHIVE，
CONFIRMED DUPLICATE/GENERATED→REMOVE。未删除任何不确定文件；未改论文正文、
算法、最终 CSV；authoritative 目录（main.tex/figures/code/submission/docs 三文件）未动。

## Moved（进入 docs/）

| 原路径 | 新路径 |
|---|---|
| FINAL_Q1_RESULT.md | docs/results/FINAL_Q1_RESULT.md |
| FINAL_Q2_RESULT.md | docs/results/FINAL_Q2_RESULT.md |
| FINAL_INTEGRATION_REPORT.md | docs/integration_report.md |
| AI工具使用详情.md | docs/compliance/AI工具使用详情.md |
| 2026年度"策联杯"…-B题.pdf | problem/official/（同名） |
| 2026年度"策联杯"…-B题-附件/ | problem/official/（同名，内部未改） |

Q3 最终报告不复制：原件在 `code/outputs/q3/q3-pro-v2/reports/Q3_PRO_V2_FINAL_REPORT.md`。

## Archived（进入 archive/，内容未修改）

- `archive/development/`：STAGE_Q1_FINAL_OR_HANDOFF、STAGE_Q1_RELATEDNESS_ALNS_HANDOFF、
  STAGE_Q2_OPTIMIZATION_HANDOFF、STAGE_Q2_ROUND3_HANDOFF、STAGE2_Q1_PLAN_HANDOFF、
  NEXT_STAGE_RECOMMENDATION、ML_READINESS、README_v11_交付说明、修改说明、
  论文获奖潜力评价报告_v11、项目目录与复现说明、Q1_Q2_压缩包分析与集成报告、
  Q1_Q2_进一步优化分析与集成报告（共 13 份）
- `archive/planning/`：B题建模技术路线与三人分工.md
- `archive/experiments/q1/`：Q1_FINAL_COMPARISON.csv、Q1_FINAL_OR_COMPARISON.csv
- `archive/experiments/q2/`：Q2_FINAL_COMPARISON.csv、Q2_ROUND2_FINAL_COMPARISON.csv、
  Q2_ROUND3_FINAL_COMPARISON.csv、ROUND2_CONTROL_MANIFEST.json、
  ROUND3_CONTROL_MANIFEST.json、Q2_求解界限与字典序修改说明.md、Q2_程序与论文说明.md
- `archive/experiments/q3/`：Q3_CLOSURE_P2_RESULTS.md、Q3_P0_P1_RESULTS.md、
  Q3_程序、求解界限与优化建议.md
- `archive/paper-drafts/`：paper/、算法/、tables/、q1_alns_revision.tex、q3_symbols_addition.tex
  （main.tex 无 \input/\includegraphics 引用；已同步 code/ALNS_Q1_README.md 第16行路径）
- `archive/development/legacy-root-tree/`：旧根级 src/、scripts/、tests/、data/、outputs/ 整树

## Deleted（仅两类，均可安全重建/完全重复）

- `main.aux`、`main.blg`、`main.log`、`main.out`：LaTeX 构建中间产物，
  xelatex→bibtex→xelatex×2 管线可完整重建；已加入 .gitignore。
  `main.bbl` 与 `main.pdf` 保守保留跟踪。
- `configs/problem.json`：与 `code/configs/problem.json` SHA-256 完全一致
  （0ef7bec7ea6c…），confirmed duplicate。

## Duplicate legacy tree（审计结论，详见 .work/ROOT_DUPLICATE_TREE_AUDIT.md 快照）

| 目录 | same | older-in-root | root-only 唯一内容 | 处理 |
|---|---|---|---|---|
| src/ | 10 | 9 | eda.py | 整树归档 |
| scripts/ | 3 | 4 | 02_run_eda.py、inspect_b_compact.py、inspect_b_sources.py | 整树归档 |
| tests/ | 6 | 0 | 210 文件为误放的旧仓库快照（含 main.pdf，82.7MB） | 整树归档 |
| data/ | 15 | 3（旧构建 JSON） | 无 | 整树归档 |
| outputs/ | 18 | 10 | 65（eda/ 全部、q1/runs b0-b1 五个 run、q2 pair 文件等） | 整树归档（唯一内容保留） |
| configs/ | 1 | 0 | 无 | 删除（完全重复） |

authoritative `code/outputs/` 未参与清理；`code/outputs/q1/final/`、`q2/best/`、
`q3/q3-pro-v2/current_incumbent/` 命名不统一问题按任务要求本轮不处理。

## Preserved because uncertain / intentionally untouched

- main.tex、main.pdf、main.bbl、references.bib、figures/、submission/、
  code/ 全树、docs 三文件、pytest.ini、requirements.txt、.gitattributes
- 未跟踪目录 `.work/`、`.sync_tmp/`（用户工作草稿，未动）

## 引用与行为核验

- 脚本 06/09/10/13 对根级 COMPARISON/MANIFEST 文件只写不读，移动无破坏；
  重跑会在根重新生成同名文件（属预期）。
- `code/src/data_pipeline.py` 的附件目录发现（rglob+“附件”优先）在清理前后
  行为一致：其搜索 ROOT 为 `code/`，官方附件本就不在搜索域内，
  移动前同样触发 FileNotFoundError（已在 c81c2d8 worktree 实测确认），
  属既有问题而非本次清理引入；01_prepare_data 的既有数据流不受影响。
- figures/ 内路径全部保持不变，未重新分类。
