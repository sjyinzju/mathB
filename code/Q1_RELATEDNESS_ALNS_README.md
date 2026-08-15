# Q1 Relatedness--Standard ALNS复现说明

正式结果位于`outputs/q1/final/`，最终指标为14770 min、120845人员分钟、89架次、
118640.1 kg、49.339%，1600/1600，Validator 0违规。

核心文件：

- `src/solver/alns.py`：五类破坏、精确MILP修复、自适应权重和SA；
- `src/solver/relatedness.py`：原始设施距离、Frozen Consensus和Context V2排序；
- `scripts/05_run_alns_multiseed.py`：同预算多种子公平试验；
- `scripts/06_finalize_q1_relatedness.py`：从已验证候选按严格词典序固化最终解。

最终winner来自标准ALNS A2对14772 min R3扩展解的热启动。完整实验参数见
`outputs/q1/final/winning_config.json`，比较见项目根目录`Q1_FINAL_COMPARISON.csv`。
