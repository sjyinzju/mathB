# 问题一 ALNS 程序说明

## 结论

在原有 B0 单设施动态规划和 B1 广义节约解的基础上，新增的 ALNS 将总飞机使用时间从
`15743 min` 降至 `15418 min`，并将架次数从 100 降至 95。最终两个 CSV 已由独立
Validator 重新计算，1600 人全部服务且无约束错误。

## 新增程序

- `src/solver/alns.py`：破坏算子、自适应权重、模拟退火接受和整数规划修复；
- `src/solver/importer.py`：把已有合法 Q1 CSV 恢复为内存路线方案；
- `scripts/04_solve_q1_alns.py`：B0、B1、ALNS、导出和 Validator 的统一入口；
- `scripts/plot_q1_alns.py`：合并多阶段日志并生成论文收敛图；
- `outputs/q1/alns_best/`：最终路线、人员分配、指标、校验报告和收敛数据；
- `archive/paper-drafts/q1_alns_revision.tex`：与真实代码一致的可用论文正文。

## 环境与运行

```powershell
python -m pip install -r requirements.txt
```

从仓库现有 B1 解开始运行推荐的两阶段 ALNS：

```powershell
python scripts/04_solve_q1_alns.py `
  --balanced `
  --initial-routes outputs/q1/runs/20260814-b1-savings/q1-routes.csv `
  --initial-assignments outputs/q1/runs/20260814-b1-savings/q1-assignments.csv `
  --run-id q1-alns-balanced `
  --promote
```

如果希望从原始数据重新生成 B0、B1 后再运行 ALNS：

```powershell
python scripts/04_solve_q1_alns.py --balanced --run-id q1-full-balanced --promote
```

快速调试可使用单阶段配置：

```powershell
python scripts/04_solve_q1_alns.py `
  --initial-routes outputs/q1/runs/20260814-b1-savings/q1-routes.csv `
  --initial-assignments outputs/q1/runs/20260814-b1-savings/q1-assignments.csv `
  --iterations 20 --time-limit 180 --repair-time-limit 2
```

## 推荐配置为什么分两阶段

第一阶段每次破坏 2--3 条路线，能较快清除低利用率余量路线；第二阶段扩大到 3--4 条
路线，使人员可以在更大的邻域内重新分批。实际运行中，第一阶段得到 15601 min，第二
阶段进一步降至 15418 min。三服务设施邻域计算成本明显增加，因此不作为默认配置。

## 最终结果文件

- `outputs/q1/alns_best/q1-routes.csv`
- `outputs/q1/alns_best/q1-assignments.csv`
- `outputs/q1/alns_best/validator.json`
- `outputs/q1/alns_best/metrics.json`
- `outputs/q1/alns_best/q1-convergence.csv`
- `figures/q1_alns_convergence.pdf`

座位利用率按“载客人公里/可用座位公里”计算，因此包含返程空载航段，不能用
`1600/(架次数×座位数)`替代。
