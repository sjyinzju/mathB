# Q2 Round-3 Handoff

## Git / controls

- frozen Round-2 branch：`codex/q2-main-integration`；closing commit `c99c1b8`；
- active branch：`codex/q2-round3-intensification`；base `c99c1b8`；
- foundation checkpoint：`c25d9e0`；
- Round-2 17,595 artifact、Round-1 17,958、canonical 19,736 均未覆盖；
- origin push 被安全审查拒绝，当前成果仅在本地 worktree/branch。

## Final result

`17,218 / 254,656 / 95 / 132,473.9 / 0.9270255505`，4000/4000，Validator PASS。
Immutable repro：`outputs/q2/runs/20260816-q2-round3-final-repro`；原子 promotion：
`outputs/q2/best`；独立 95-flight artifact：`outputs/q2/best-95-flight`。

## Search evidence

- 96-flight audit 与 absorption ranking：`outputs/q2/round3-audit-17595/`；
- final 95-flight audit：`outputs/q2/round3-audit-final/`；
- forced 4/5/6 absorption 没有找到 95；generic 6-route 成本失控；
- actual 96→95 是 s402 iteration 124 的 low-utilization 4-route exact repair；
- s401/s402 global restarts 分别贡献 186/178 min；
- targeted 5-route 贡献 1 min；path relink V2/cross-exchange 当前 basin 无 primary gain；
- LAND-heavy 在 s401/s402 贡献 60 min；
- final primary 17,218 来自 exploration run，但 selected useful primary columns 都是
  incumbent sequences；不宣称 ML 或 random exploration 已优化 solver。

## ML V2

`outputs/q2/ml-data-round3/`：663,504 rows、309,785 exact、1,863 positives、89 novel
positives、3 lineage roots；novel train/validation/test = 26/51/12。Decision：READY。
本轮没有训练模型。下一阶段先 LR，再 LightGBM ranking A/B。

## Resume / safety

长 run 在 `runs/<id>/checkpoints/iter-*` 保存 best CSV、current/best metrics、temperature、
operator weights 与 stagnation。安全 resume 语义是从 immutable best 重启；没有序列化
SciPy/HiGHS 内部状态。large candidate logs 与 `candidate_events.csv` 不应提交。

## Still rejected

Repeated visits、generic 6-route、heuristic CG、Full Branch-and-Price、UCB、Deep ML。

