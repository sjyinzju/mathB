# v11 交付说明

## 主要文件

- `main.tex`：最新版可直接放在项目根目录编译的论文正文。
- `deliverables/final_v11/main_v11.tex`：同内容版本留档。
- `code/`：Q1、Q2、Q3 统一源码、数据、正式结果和验证程序。
- `Q1_Q2_进一步优化分析与集成报告.md`：两个新压缩包的新增内容、算法合理性与论文对齐说明。
- `论文获奖潜力评价报告_v11.md`：当前论文水平、风险与提交建议。
- `figures/`：更新后的 Q1 路线池改进图、Q2 第三轮改进图和最终代表性路线图。

## 正式结果

| 问题 | 飞机时间/min | 人员在途/min | 架次数 | 燃油/kg | 利用率 | 覆盖/服务 |
|---|---:|---:|---:|---:|---:|---:|
| Q1 | 14730 | 121363 | 89 | 118624.4 | 49.430% | 1600/1600 |
| Q2 | 17218 | 254656 | 95 | 132473.9 | 92.703% | 4000/4000 |
| Q3 第一阶段 | 29659 | 241073 | 168 | 235329.4 | 55.995% | 3840/3840 必选人员 |
| Q3 第二阶段 | 29659 | 250494 | 168 | 235329.4 | 58.207% | 158/160 临时人员 |

## 推荐复现顺序

在项目根目录执行：

```bash
cd code
python scripts/validate_solution.py --question q1 \
  --routes outputs/q1/final/q1-routes.csv \
  --assignments outputs/q1/final/q1-assignments.csv --json
python scripts/validate_solution.py --question q2 \
  --routes outputs/q2/best/q2-routes.csv \
  --assignments outputs/q2/best/q2-assignments.csv --json
python scripts/validate_solution.py --question q3 \
  --routes outputs/q3/best/q3-routes.csv \
  --assignments outputs/q3/best/q3-assignments.csv --json
```

重绘 Q1/Q2 阶段图：

```bash
python scripts/plot_integrated_improvements.py \
  --q1 ../Q1_FINAL_OR_COMPARISON.csv \
  --q2 ../Q2_ROUND3_FINAL_COMPARISON.csv \
  --output-dir ../figures
```

若系统未安装中文字体，脚本会自动使用英文标签以避免方框乱码。在 Windows 上可安装微软雅黑/黑体，或用 `--font 中文字体文件.ttf` 指定字体后重新生成中文图。

论文编译应从项目根目录执行：

```bash
latexmk -xelatex -bibtex main.tex
```

## 界限解释

- Q1 的 13337 min 是原问题放松下界；14208.637 min 只是当前有限路线池 LP 参考界。
- Q2 的历史 16296 min 只属于固定候选 RMP；第三轮局部 gap=0 只属于一次有限 4 路线邻域。
- Q3 的 14125 min 是机场--机型分层连续多商品流全局下界；15198 min 只是有限候选池参考界。

