# INTEGRATION APPROVAL PLAN（共享性能基座统一 + 算法分支集成审计）

日期：2026-08-15 ｜ 状态：待批准 ｜ 批准方式：回复"批准 clustering" / "批准 ALNS@7d04432" / "两个都批准"

## 当前基线
- main = `d28e982`（已吸收 performance/shared-infrastructure，15,371 gate 全过）
- codex/q1-clustering = `94d08c2`（merge-base 与 main：`1bfa548`）
- ALNS 审计基线 = `7d04432`（origin/platinumist_update 当前 HEAD 为 `c37ea9b`，其后 Q2 commits 本轮未纳入）

## 批准项 1：clustering —— main 合入 codex/q1-clustering
- 演练已完成：临时分支 `audit/q1-clustering-main-integration`（worktree `%TEMP%\clustering-integration`），merge commit `7b900ae`
- 冲突语义解决：improve.py 保留 clustering 的 MergeSearchResult/候选计数/事件日志并接入 SolverCache，同时保留 main 的 relocation/ejection；__init__.py 双导出合并
- 验证：pytest 50 全过；k=3/budget=25 代表性回归与 Phase-1 存档**完全一致**（15,683/99 班次/120,949；candidate_events 与 routes 字节一致；decision 同为 abandon_mainline）
- 批准后执行：在 codex/q1-clustering 上 merge main，按演练相同方式解决冲突，重跑 50 项 pytest + b25 对照

## 批准项 2：ALNS —— main 合入 7d04432 基线
- 演练已完成：临时分支 `audit/q1-alns-main-integration`（worktree `%TEMP%\alns-audit-7d04432`），merge commit `8263d51`
- 冲突语义解决：13 处 add/add 共享文件取 main；删除 importer.py（load_q1_solution 统一到 exporter）；alns.py 与 scripts/04 的 augmentation 层重接 SolverCache；RouteVariant/variant_cache 保留（MILP repair-context 专属）
- 验证：pytest 41 全过；固定 seed=0 / iterations=12 / 同一初解的 before/after 对照**语义完全一致**（operator 序列、accepted、best 轨迹相同；assignments/operator_stats/metrics 字节一致），仅提速 2.5×（54.7s→21.8s）
- 批准后执行：将 `8263d51` 的融合方式应用到正式分支

### ALNS 批准的风险提示（必须先决策）
origin/platinumist_update = `c37ea9b` 包含 7d04432 之后的 Q2 commits（本轮未审计）。批准项 2 的执行方式二选一：
- **A（推荐）**：仅在 7d04432 基线上落地融合结果（新建分支，如 `platinumist_update_alns_base`），**不动** platinumist_update 现有 HEAD；Q2 commits 待下一轮单独审计后再决定合流
- B：将 main 合入 platinumist_update HEAD（c37ea9b）——会把未审计的 Q2 commits 一并带入统一基座，本轮证据不覆盖，不建议

## 明确不在本轮范围
- clustering 与 ALNS 的相互融合
- 对 candidate ordering / objective / acceptance rule / 随机调用顺序的任何改动
- ALNS golden 配置改从 15,371 起步的 multi-seed 重跑（列入下阶段）
- push / rewrite / 删除任何正式分支

## 审计产物位置
| 项 | 位置 |
|---|---|
| clustering 演练分支 | worktree `%TEMP%\clustering-integration`，branch `audit/q1-clustering-main-integration` @ `7b900ae` |
| clustering 回归 | worktree 内 `outputs/q1/clustering/audit-integration-b25`、`audit-integration-k3` |
| ALNS 演练分支 | worktree `%TEMP%\alns-audit-7d04432`，branch `audit/q1-alns-main-integration` @ `8263d51` |
| ALNS before/after | worktree 内 `outputs/q1/runs/audit-before-smoke`、`audit-after-smoke` |
