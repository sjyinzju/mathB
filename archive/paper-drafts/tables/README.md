# Tables

建议由 Python 根据最终校验结果自动生成 LaTeX 表格，例如：

- `final_metrics.tex`：三问五项指标；
- `baseline_comparison.tex`：基线与本文算法对比；
- `ablation.tex`：消融实验；
- `sensitivity.tex`：敏感性分析。

在 `main.tex` 中用 `\input{tables/final_metrics.tex}` 引入，避免手工录入导致论文与提交 CSV 数字不一致。
