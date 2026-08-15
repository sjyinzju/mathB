# Q2精确局部MILP与多重启复现说明

正式结果位于`outputs/q2/best/`，最终指标为17595 min、259487人员分钟、96架次、
135954.1 kg、91.021%，4000/4000，Validator 0违规。

核心文件：

- `src/solver/q2.py`：候选路线RMP与逐航段容量；
- `src/solver/q2_lns.py`：4路线破坏、1--5设施有界列、局部MILP、SA和精英重组；
- `src/solver/q2_flow.py`：有向OD流图与结构候选；
- `src/solver/q2_round2.py`：精英池、差异路径重组和局部分支门；
- `scripts/06_optimize_q2_lns.py`：精确LNS入口；
- `scripts/07_recombine_q2_elites.py`：精英重组入口。

正式`best`是不可变复现运行`outputs/q2/runs/20260815-q2-round2-final-repro/`的原子副本。
其0迭代配置用于逐字节复现最终CSV；真实180 s winning search的参数和来源哈希记录于
`best/run_config.json`。16296和5.8651%的证明范围分别限于旧RMP和一次局部主问题。
