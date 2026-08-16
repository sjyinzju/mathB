# Result Provenance（最终整合 2026-08-16）

三问最终解全部来自已验证 run 的原始导出 CSV，整合过程未做任何手工数值修改。
每条记录给出来源分支、run 标识、文件 SHA-256 与 Validator 摘要。

## Q1（14730 min / 121363 / 89 架次 / 118624.4 kg / 49.430% / 1600 人）

- 来源分支：`codex/q1-final-or-intensification` @ `be109e6`
- 源 run：`final14730-control-s21/seed-21`（多种子公平试验的获胜配置）
- 产物目录：`code/outputs/q1/final/`
- 提交文件：
  - `q1-routes.csv` SHA-256 `715a48275c1bac1881d7a33cc2ca20c9a4c6039f2670533d968013ea026ec2bf`
  - `q1-assignments.csv` SHA-256 `ca37015a16a43b05270d9f66d0acfa8bd62aa67a00a9efa580661332d0de8fad`
- Validator：`valid=true, issues=0`（`validator.json`；2026-08-16 复跑一致）
- 证明状态：无原问题全局最优证书；14208.637 min 为不完备路线池 LP 参考界，
  池内间隙 3.669%，不作为全局最优性证明。

## Q2（17218 min / 254656 / 95 架次 / 132473.9 kg / 92.703% / 4000 人）

- 来源分支：`codex/q2-round3-intensification` @ `4a381e3`
- 源 run：`20260816-q2-round3-final-repro`（immutable authoritative；
  `best/run_config.json` 与 repro run 保持字节一致，相对路径可移植）
- 产物目录：`code/outputs/q2/best/`
- 提交文件：
  - `q2-routes.csv` SHA-256 `fed7a5f6e8d30ff05b2c4309df38d72c5e0d5025cb6a957088dcefb17f3c2a65`
  - `q2-assignments.csv` SHA-256 `5d4af061ecf5affb0185d57a62b3e1ae61aefd87a99b649a0b897edb48c65b70`
- Validator：`valid=true, issues=0`（`q2-validator.json`；2026-08-16 复跑一致）
- 96→95 架次来自普通低利用率 4 路线精确修复（4→3 吸收）；ML 数据仅离线建模
  准备，未训练模型，不把探索标签当作收益原因。

## Q3 Stage1（T0 = 28728 min / 242455 / 162 架次 / 226835.3 kg / 57.840% / 3840 人）
## Q3 Stage2（28728 min / 251777 / 162 架次 / 226835.3 kg / 60.098% / 3997 人，临时 157/160）

- 来源分支：`codex/q3-pro-v2` @ `be58e57`（formal run 代码 commit `b71dad4`）
- 源 run：`q3-pro-v2-deep-v1`；墙钟 9763.63 s
- 产物目录：`code/outputs/q3/q3-pro-v2/current_incumbent/`
- 提交文件（Stage2 最终）：
  - `q3-routes.csv` SHA-256 `43ae68ca0fd5dbec8ddcd9cb412d70d65cf1eb32fbbf9fa2c693ad38f2eff230`
  - `q3-assignments.csv` SHA-256 `41051991112ebadda3393044147f11fb3e199c6557f97e7e429276f7201d9e6a`
- Stage1 基准文件：
  - `q3-base-routes.csv` SHA-256 `43ae68ca0fd5dbec8ddcd9cb412d70d65cf1eb32fbbf9fa2c693ad38f2eff230`
  - `q3-base-assignments.csv` SHA-256 `2f315a5ca9bf802dd0de9e197f5a1ee5bc7bdc19feb5d225f5113caf9f8f1b76`
- Validator：两阶段均 `valid=true, issues=0`（`q3-base-validator.json`、`q3-validator.json`；
  2026-08-16 复跑一致，Stage2 served=3997、unserved_optional=3：P1102/P2239/P3290）
- 界限（`bounds.json`）：全局 LB 14125（分层连续多商品流 14124.895 上取整），
  UB 28728，认证区间 50.8319%；3116 池 LP 15197.6776、定价缩减池受限主问题
  17217.4167 均为池内参考（后者 `valid_for_original_problem=false`）；
  Stage2 served=157 为 fixed-structure 最优，unrestricted 158/159/160 状态 `open`。
- 演进链（全部有 run 产物支撑）：
  - 30546：`code/outputs/q3/runs/20260815-q3-v6-deep/`（旧深度解，173 架次）
  - 29659：`code/outputs/q3/closure_p2_best/`（Closure--P2，168 架次）
  - 29155：`code/outputs/q3/best/`（PRO V1，165 架次）
  - 28868：`q3-pro-v2/parameter-screen.csv`、`deep-islands.csv`（40 screen runs：
    22@29155 / 13@29006 / 5@28868；四岛均收敛 28868）
  - 28728：`q3-pro-v2/optional-rescue-dossier-v2.json`、`final-feedback.json`

## 整合操作记录

- 论文基线：`platinumist_update` @ `181d0e2`
- 整合分支：`final/paper-q123-integration`，选择性 checkout 各分支代码/产物，
  未执行破坏性 merge；全程无 force push，main 未改动。
- `.gitattributes` 强制 csv/json/jsonl/md/py 以 LF 存储，保证字节比对测试在
  autocrlf 环境下通过。
- 全部 96 项 pytest 通过（2026-08-16，72.3 s）。
