# Q1 Final OR / Matheuristic Intensification Handoff

## Git 与 frozen control

- Frozen branch/control：`codex/q1-relatedness-alns` @ `38e2347`
- Frozen metrics：14,770 / 120,845 / 89 / 118,640.1 / 0.4933894869 / 1600 / VALID
- Active branch：`codex/q1-final-or-intensification`
- Stage commits before final result commit：
  - `293df8c feat(q1): add exact elite route-pool master`
  - `0408bf8 feat(q1): intensify route-pool recombination and repair`

Frozen branch was not modified. Q2/Q3/main were not merged or changed.

## 实现入口

- `src/solver/q1_or.py`：route identity、elite route pool、allocated-pattern exact master/LP、targeted repair、route audit。
- `src/solver/alns.py`：可选 mild reheating；默认行为不变。
- `scripts/07_run_q1_route_pool_master.py`：pool/master/reproduction/LP。
- `scripts/08_run_q1_targeted_intensification.py`：elimination、cross-exchange、high-impact/block/large neighborhoods。
- `scripts/09_run_q1_elite_recombination.py`：A2×R3 / elite recombination。
- `scripts/10_run_q1_path_relink.py`：轻量 difference-region path relinking。
- `scripts/11_run_q1_repair_challengers.py`：Regret-k / Beam / MILP A/B 与 CP-SAT decision。
- `scripts/12_build_q1_ml_foundation.py`：pre-outcome features、labels、run/lineage manifests、readiness。
- `scripts/13_finalize_q1_or.py`：严格 comparator、validator gate、immutable hashes 与 final promotion。

## 结果轨迹

| 阶段 | Primary | Passenger | Flights | 备注 |
|---|---:|---:|---:|---|
| Frozen control | 14,770 | 120,845 | 89 | VALID |
| Round 1 master | 14,743 | 120,919 | 89 | 首次跨轨迹 exact recombination |
| Round 2 flight cap | 14,732 | 123,171 | 88 | 最低 flights |
| Cross-exchange | 14,732 | 123,113 | 88 | secondary only |
| Round 4 master | **14,730** | 122,494 | 89 | final primary best |
| Standard education | **14,730** | **121,363** | 89 | final lexicographic winner |
| Round 5 strict UB 14,729 | — | — | — | 600 s no incumbent; not proof |

最终 winner：**Master-recombined + Standard ALNS education**。

## 关键实验判断

- Master reproduction：PASS exact。
- Pool：Round 4 为 95 sources、391 unique physical routes、996 allocated columns；Round 5 为 98 sources、391 routes。
- LP：14,208.636981；restricted gap 3.6693%，不是 global gap。
- 89→88：成功，best 88-flight primary 14,732。
- A2×R3 child / path relinking：最好 14,770，无刷新。
- Cross-exchange：只改善 passenger time 58。
- High-impact / block / 6–10 route neighborhoods：无 primary 收益。
- Islands：单体未胜，但 routes 回流促成 Round 4 14,730，作为 contributor 有效。
- Pricing：未进入；missing columns 尚无主瓶颈证据；B&P reject/not justified。
- Regret/Beam：不胜 MILP；CP-SAT reject；mild reheating identical reject。
- Duplicate memory：轻量使用，避免短期重复同一失败 neighborhood；不永久禁用。
- Relatedness：diversification、soft guidance、pool contributor。
- ML：19 evaluated events / 1 positive / 1 lineage，NOT_READY；没有训练模型。

## 最终产物与验证

- `outputs/q1/final/q1-routes.csv`
- `outputs/q1/final/q1-assignments.csv`
- `outputs/q1/final/metrics.json`
- `outputs/q1/final/validator.json`
- `outputs/q1/final/winning_config.json`
- `outputs/q1/final/method_metadata.json`
- `outputs/q1/final-or/round4-strict-after-feedback/`
- `outputs/q1/final-or/round5-final-strict-below-14730/no-incumbent.json`
- `outputs/q1/final-or/ml-data/`

Final hashes：routes `148142c883094d8edd6c40b57ed8dfe7d205aeca86ee7c9d0ec08bc0317faf78`；assignments `3875c5b279071cdebce7d9038f0d687d29d41349cf11a0e4c5e924a79f40f977`。

## 复现与维护注意

Master 的 MIP status=1 表示时间限制下有 incumbent，不是 proven optimal。Round 5 无 incumbent 也不是 infeasibility。不得将 restricted LP / MIP bound 写成 Q1 global bound。下一位维护者若继续，只做 `NEXT_Q1_STAGE_RECOMMENDATION.md` 所述单一聚焦实验；不要重启 clustering、weight/SA grids 或直接训练 ML。
