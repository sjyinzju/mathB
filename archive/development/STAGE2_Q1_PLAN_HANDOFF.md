# Q1 Solver 阶段交接

## 当前状态

Q1 已进入可执行基线阶段。Stage 1 的原始距离、清洗需求、规则函数和独立 Validator 保持不变；新增共享路线模型、技术经停多标签搜索、内存评价器、单设施容量/LAND 分流 DP、人员回填、官方 CSV 导出和实验运行器。

基线 B0 按目的设施分别构造航线。每个 LAND 人员仍是独立可分配流，不会把整条 OD 固定在一个机场。每条候选航线均使用原始相邻距离，枚举 T1/T2/T3，搜索最多 5 次海上停靠和合法加油决策。

## 核心接口

- `augment_service_sequence(...)`：服务顺序 → 完整闭合航线及加油见证。
- `evaluate_route(...)`：独立于 CSV Validator 的内存燃油、时间、载客和指标模拟。
- `solve_q1_baseline(...)`：构造 1600 人 B0 解。
- `export_q1_solution(...)`：输出官方 routes/assignments 字段。

共享 `PassengerAssignment` 使用上下客 occurrence，不把 Q1 的机场上机规则写死在路线内核中，后续可以扩展 Q2。

## 运行与验收

```powershell
python -m pytest -q
python scripts/03_solve_q1_baseline.py --promote
python scripts/03_solve_q1_baseline.py --savings --promote
python scripts/validate_solution.py --question q1 --routes outputs/q1/best/q1-routes.csv --assignments outputs/q1/best/q1-assignments.csv
```

阶段门必须同时满足：1600/1600 人恰好分配一次、Validator PASS、内部与 Validator 五项指标一致、同配置重复运行可复现。每次求解结果写入 `outputs/q1/runs/<run_id>/`，合法且更优的解才进入 `outputs/q1/best/`。

## 三人下一步

| 角色 | 当前任务 | 下一阶段 |
|---|---|---|
| 工程/数据 | 维护评价器、B0、导出和运行记录 | 缓存、增量评价、实验自动化 |
| 算法 | 复核技术经停 DP 与 LAND 分流 | Generalized Savings、Insertion、局部搜索 |
| 论文/实验 | 独立核对 Validator 与边界测试 | 消融表、流程图、复杂度和敏感性分析 |

## 禁止事项

- 不使用距离最短路闭包替代原始航段。
- 不在预处理阶段固定 LAND 机场或不可逆人员批次。
- 不用加权总分牺牲总飞机时间。
- 聚类只能排序候选，不能硬切设施簇。
- ALNS、Bandit 和机器学习不得阻塞 B0/B1；任何学习模型都不能替代精确规则评价和最终 Validator。
