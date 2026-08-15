# Q2 深度优化阶段交接

日期：2026-08-15

分支：`codex/q2-main-integration`

公共基座：`main@eabfcfe`

## 最终状态

本阶段从已验证的 Standard ALNS 控制 18,906 分钟继续优化，最终
`outputs/q2/best` 原子复制自 immutable run
`outputs/q2/runs/20260815-q2-final-repro-s2`。该复现 run 记录
`git_commit=da0d87d`，其 routes、assignments 与 metrics SHA 均与正式 seed 2
原始 run 完全一致。

| 指标 | 最终结果 | 19,736 RMP | 18,906 control |
|---|---:|---:|---:|
| 飞机总使用时间 | **17,958 min** | -1,778 (-9.01%) | -948 (-5.01%) |
| 人员总在途时间 | 263,588 min | -7,146 | -2,720 |
| 架次数 | 97 | -10 | -6 |
| 燃油 | 138,075.2 kg | -14,835.2 | -8,367.3 |
| 座位利用率 | 0.8974631474 | +0.0791648920 | +0.0437596474 |
| 服务 / Validator | 4,000 / PASS | PASS | PASS |

独立 Validator 返回 0 issues，内部 metrics 与 Validator 完全一致。
历史 separate baseline 为 44,184 分钟、258 架次、353,993.8 kg，来源仅作
比较的 `origin/platinumist_update:code/outputs/q2/separate`，未迁移进当前输出树。

## 决策与改进轨迹

严格主目标轨迹为：

`18,906 → 18,607 → 18,547 → 18,269 → 18,180 → 18,151 → 18,104 → 17,958`。

- **Larger exact neighborhood — ADOPT。** 同 90 秒三种子，fixed 4-route
  best/median/worst 为 18,607/18,696/18,851，胜 fixed 3-route 的
  18,746/18,816/18,855。4-route 的吞吐较低，但能删除整架次并跨越原 basin。
- **Adaptive destroy size — REJECT。** 2/3/4-route 规则为
  18,852/18,866/18,877，过度回退到小邻域；固定 4-route 更强。
- **Targeted 4-stop — ADOPT。** 首次门禁中一条新 4-stop column 所在 accepted
  repair 同时删架次并贡献 129 分钟；最终五种子又有 6 次 accepted 4-stop
  选择。3-stop 在终验中有 5 条新列被采用；5-stop 保留先前一次真实证据，
  但本轮终验没有新增选择。
- **Multi-route ejection chain — REJECT 为默认算子。** 三种子
  18,649/18,802/18,880；它确实能删路线，但 best/median 均不及普通 4-route
  exact repair。保留 simple route ejection，其有效性已再次由最终 winner 的
  一次删架次证明。
- **Elite recombination — ADOPT。** 三个不同 partner 的
  18,547/18,576/18,583 全部改善 18,607；后续 final elite pool 把
  18,180 依次降至 18,151 和 18,104。最终 pool polish 未再超过 17,958。
- **SA acceptance — ADOPT。** 同 90 秒三种子 best/median/worst
  18,579/18,596/18,767，胜 strict 的 18,607/18,696/18,851。实际接受的是
  aircraft time 相同但次级略差的结构，没有接受更差主目标；这些结构帮助后续
  exact repair 进入新 basin。
- **Context ranker — OPTIONAL，不作默认。** best 18,269 比 geometry 的
  18,285 好 16 分钟，但 median 18,483 劣于 18,427。它可作多样化启动，默认仍
  用 geometry ranker。
- **Local candidate cache — REJECT。** 固定 6 次 repair 的 CSV 与轨迹完全
  一致，但仅从 70.644s 降到 69.557s（1.56%），复杂度不值得保留。
- **UCB1 Bandit — REJECT。** best/median/worst 18,197/18,245/18,269，
  全面不及 adaptive roulette 的 18,180/18,185/18,199。
- **ML — NOT READY，未训练。** 三个 context runs 有 52,814 行，其中
  49,390 行为 censored、3,424 个 exact-evaluated variants、77 个 MILP-selected，
  仅 45 个 useful positive。按 run/seed 分组无法形成可靠 train/validation/test；
  未把 censored 样本污染成 negative，也未强行训练 LR/RF/LightGBM。
- **Repeated visits — 不进入。** distinct 3/4/5-stop 搜索仍有明确收益，日志没有
  证明重复 occurrence 的必要性。Heuristic column generation 与完整
  Branch-and-Price 继续 REJECT。

## 最终算法与终验

最终 winning pipeline：Shared Solver Core 上的 fixed 4-route Adaptive ALNS；
四个已证明 destroy operators 由 adaptive roulette 选择；候选使用 geometry-ranked
bounded 1–5 stop sequences，并在大邻域给少量 targeted 4-stop 配额；local repair
由 exact MILP 完成；SA 只改变 acceptance；阶段间使用小型 diverse elite pool 做
elite-difference exact recombination。所有物理、燃油、technical stop、evaluator、
materialization 和 Validator 仍复用 main Shared Core。

最终 ALNS 配置：90 秒 wall-clock，local primary 8 秒，secondary 0，24 sequences，
fixed destroy size 4，max service stops 5，adaptive reaction 0.2，SA temperature
12、cooling 0.92、minimum 0.5。五种子结果为
18,010/18,048/17,958/18,043/18,102；best/median/mean/worst/std 为
17,958/18,043/18,032.2/18,102/47.4063，5/5 击败 18,906。

终验平均实际耗时 94.57 秒，平均 15.8 iterations，折算约 50.19 iterations/300s；
合计 8,041 evaluator calls、9,729 technical-stop augmentation misses、72 个
accepted moves、1 次 route ejection、6 次 selected 4-stop。完整终验记录了
193,540 candidate rows，其中 15,887 exact-evaluated、314 MILP-selected。

## 产物与边界

- 最终说明：`FINAL_Q2_RESULT.md`
- 对比表：`Q2_FINAL_COMPARISON.csv`
- 五种子明细：`outputs/q2/final-benchmark.csv`
- ML gate：`outputs/q2/ml-readiness.json`
- 最终 CSV：`outputs/q2/best/q2-routes.csv`、`q2-assignments.csv`

`outputs/q2/baseline-19736` 保持不变。Q1/Q3、Deep Learning、完整
Branch-and-Price 均未进入。本阶段到此停止，等待审查与后续是否合回 main 的单独决定。
