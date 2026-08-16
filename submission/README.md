# submission/ —— 正式提交文件

本目录为三问正式提交的 6 份官方格式 CSV（与 `code/outputs/` 中权威产物逐字节一致）：

| 文件 | 来源 | 校验 |
|---|---|---|
| `q1-routes.csv` / `q1-assignments.csv` | `code/outputs/q1/final/` | Validator 0 issues；1600/1600 人 |
| `q2-routes.csv` / `q2-assignments.csv` | `code/outputs/q2/best/` | Validator 0 issues；4000/4000 人 |
| `q3-routes.csv` / `q3-assignments.csv` | `code/outputs/q3/q3-pro-v2/current_incumbent/` | Validator 0 issues；3997 有效 + 3 留空 |

SHA-256 见 `docs/result_provenance.md`。

## 提交打包说明（按官方格式规范）

- 论文 PDF：由仓库根 `main.tex` 编译生成（`main.pdf`），按三位参赛队号命名，
  ≤20 MB。PDF 不入 git（编译产物），提交前现场编译。
- 支撑材料压缩包：含 `code/`（源码+产物）、`AI工具使用详情.md` 等，按三位参赛队号
  命名，≤20 MB。打包时注意压缩包内不得出现姓名/学校等身份信息。
- Q3 第一阶段基准文件（`q3-base-routes.csv` / `q3-base-assignments.csv`）如需随
  支撑材料提交，位于 `code/outputs/q3/q3-pro-v2/current_incumbent/`。
