# Reproduction Guide

## 环境

- Python 3.11.9（开发/验证环境）；依赖见 `code/requirements.txt`：
  `matplotlib>=3.8`、`numpy>=1.26`、`scipy>=1.13`、`pytest>=8.0`。
- 安装：`pip install -r code/requirements.txt`
- 论文编译：xelatex + bibtex（MiKTeX/TeX Live 均可）。
- 仓库根目录的 `pytest.ini` 已把测试路径指向 `code/tests`、源码路径指向 `code/`，
  在仓库根执行 `python -m pytest` 即可。

## 路线一：validate-existing（推荐先跑，秒级）

验证仓库内已固化的最终 CSV 与测试全部通过：

```powershell
python -m pytest                       # 预期 96 passed
cd code
python scripts\validate_solution.py --question q1 --routes outputs\q1\final\q1-routes.csv --assignments outputs\q1\final\q1-assignments.csv
python scripts\validate_solution.py --question q2 --routes outputs\q2\best\q2-routes.csv --assignments outputs\q2\best\q2-assignments.csv
python scripts\validate_solution.py --question q3 --routes outputs\q3\q3-pro-v2\current_incumbent\q3-base-routes.csv --assignments outputs\q3\q3-pro-v2\current_incumbent\q3-base-assignments.csv
python scripts\validate_solution.py --question q3 --routes outputs\q3\q3-pro-v2\current_incumbent\q3-routes.csv --assignments outputs\q3\q3-pro-v2\current_incumbent\q3-assignments.csv
```

四条命令均输出 `"valid": true`。

## 路线二：full（完整重跑，小时级）

各问入口脚本（全部在 `code/scripts/`，详细参数见脚本 `--help` 与附录代码清单）：

1. **Q1**：`04_solve_q1_alns.py` → `05_run_alns_multiseed.py` →
   `06_finalize_q1_relatedness.py` → `07_run_q1_route_pool_master.py` …
   `13_finalize_q1_or.py`（产物固化到 `outputs/q1/final/`）。
2. **Q2**：`05_solve_q2.py` → `06_optimize_q2_lns.py` → `07_recombine_q2_elites.py` →
   `11_audit_q2_round3.py` / `12_search_q2_absorption.py` / `13_summarize_q2_round3.py`。
3. **Q3**：`06_solve_q3.py` → `07_finalize_q3_results.py` →
   `08_compute_q3_enhanced_bounds.py` → `09_optimize_q3_p0_p1.py` →
   `10_resume_q3_p2.py` → `11_finalize_q3_p2_feedback.py` →
   `12_run_q3_pro.py` → `13_run_q3_pro_v2.py` → `14_run_q3_pro_v2_robustness.py`
   （最终 incumbent 输出到 `outputs/q3/q3-pro-v2/current_incumbent/`；
   PRO V2 正式 run 墙钟约 2.7 小时，各脚本内置预算保护）。

每步运行结束会自动调用 Validator 落盘 validator.json；任一环节违规即终止。

## 图片重生成

论文 `figures/` 全部由 CSV 可复现生成：

```powershell
cd code
python scripts\plot_q3_results.py --routes outputs\q3\q3-pro-v2\current_incumbent\q3-routes.csv --assignments outputs\q3\q3-pro-v2\current_incumbent\q3-assignments.csv --output-dir ..\figures
python scripts\plot_q3_search_evolution.py --output-dir ..\figures
```

`plot_q1_alns.py`、`plot_q2_results.py`、`plot_integrated_improvements.py` 需要
指向对应 metrics/CSV 的必填参数，具体以各脚本 `--help` 输出为准（输入均为
`code/outputs/` 下已固化的产物）。

`q3_search_evolution` 的五个数值（30546/29659/29155/28868/28728）来源见脚本
docstring 中列出的仓库内产物文件。

## 论文编译

```powershell
latexmk -xelatex -bibtex main.tex
# 或手工管线：xelatex main.tex → bibtex main → xelatex main.tex → xelatex main.tex
```

注意：MiKTeX 25.12 起不再附带 perl，`latexmk` 需要系统自行安装 perl；无 perl 时
直接使用上面的手工管线，结果等价。MiKTeX 首次编译会自动安装缺失宏包。附录
`\inputcode` 直接读取 `code/` 下源码，因此编译目录必须与 `code/` 同级（即仓库根）。
