# Q2 Round-2 Final Intensification 交接

日期：2026-08-15

分支：`codex/q2-main-integration`

公共基座：`main@eabfcfe`

## 最终状态

`outputs/q2/best` 已原子 promotion 自
`outputs/q2/runs/20260815-q2-round2-final-repro`；其真实 winning search source 为
`outputs/q2/runs/20260815-q2-round2-extended-round1-control-s30`：

`17,595 aircraft min / 259,487 passenger min / 96 flights / 135,954.1 kg / 0.9102123546`

4,000/4,000，独立 Validator PASS、0 issues，内部 metrics 完全一致。全量测试
62/62 PASS。Round-1 17,958 run 与 baseline-19736 未覆盖。

## 搜索轨迹

严格 primary 轨迹：

`17,958 → 17,853 → 17,837 → 17,798 → 17,765 → 17,749 → 17,740 → 17,693 → 17,679 → 17,665 → 17,656 → 17,634 → 17,595`

最后的 17,595 不是 Round-2 finalist 的结果，而是从 17,656 elite 用 Round-1
geometry configuration extended continuation 得到。第 26 次 low-utilization 4-route
repair 删除一条架次，primary gain 61；后续 19 repairs 无 primary gain 后停止。

## Fair benchmark

同起点、seeds 11/12/13、90 秒目标：

- control：17,693 / 17,693 / 17,688，best/median 17,688/17,693；
- portfolio：17,679 / 17,693 / 17,693，best/median 17,679/17,693；
- finalist：17,671 / 17,671 / 17,667，best/median 17,667/17,671。

finalist 在短预算稳定胜出，但 extended control 最终通过 route elimination 反超。这一
结果说明复杂 operator 对早期 intensification 有用，但当前最强 late basin 仍可由普通
4-route exact neighborhood 榨取。

## ADOPT / REJECT

ADOPT：

- extended multi-restart；
- quality-constrained diverse elite pool；
- objective-near exact difference recombination；
- global-best restart；
- cross-exchange；
- targeted 5-route 作为低频 secondary/intensification；
- candidate event schema、exploration logging、run-group split 数据层。

REJECT / 不作默认：

- unbounded diversity-heavy partner；
- dedicated 5-route flight-elimination operator；
- fix-and-optimize 当前窗口；
- Local Branching 当前 master 上的实现；
- geometry+context portfolio 作为默认 ranker；
- repeated visits；
- heuristic CG、Branch-and-Price、UCB、任何 ML training。

## Local master 与合法性边界

所有 exact repairs 仍复用 main Shared Solver Core、technical-stop、physics、evaluator、
materialization 与 Validator。restricted dual/gap 只覆盖相应局部有限候选池。最终
route-elimination repair 的 restricted gap 为 0.058651，不能解释为 Q2 全局 gap。

## ML foundation

`outputs/q2/ml-data`：136,597 candidates；12,467 exact-evaluated；12,343 true
negatives；72 positives；52 invalid；124,130 censored。6 runs 按 run 分组，
train/validation/test positives 32/24/16，0 duplicate candidate IDs、0 run leakage。

当前 gate 为 NOT_READY：positive 仍主要是 incumbent sequence variants，探索候选虽被
精确评价但缺少 useful positive。最可信的现有 target 是“在 exact-evaluated 集合内，
variant 是否被 useful accepted repair 选择”，但还不能可靠外推到 novel ranking pool。

## 下一阶段

不建议立即训练 ML。若继续 Q2，优先做一个严格有界的 96→95 flight absorption 阶段：
围绕最终 96-flight incumbent 的低利用率/共享设施 route，使用 quality elite restarts
和 exact 4–5-route windows；同时继续小比例 exploration logging，直到非 incumbent
positive 跨多个 run 出现。若该阶段无新 route elimination 或 primary trend，则冻结 Q2。

不要 merge main、不要开始 Q3，等待单独批准。
