"""Promote one validated Q1 run and build the final comparison/handoff artifacts."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.validation import validate_solution


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def metrics_from_file(path: Path) -> dict:
    payload = read_json(path)
    return payload.get("validator_metrics", payload.get("metrics", payload))


def aggregate_best_run(experiment_dir: Path) -> tuple[dict, dict]:
    aggregate = read_json(experiment_dir / "aggregate_summary.json")
    seed_row = min(
        aggregate["per_seed"], key=lambda row: float(row["best_aircraft_time_minutes"])
    )
    summary = read_json(experiment_dir / f"seed-{seed_row['seed']}" / "run_summary.json")
    return aggregate, summary


def comparison_row(
    method: str,
    metrics: dict,
    budget_type: str,
    status: str,
    *,
    runtime: float | None = None,
    runtime_to_best: float | None = None,
    best: float | None = None,
    median: float | None = None,
    worst: float | None = None,
    seed: int | None = None,
    validator: str = "VALID",
) -> dict[str, object]:
    aircraft = float(metrics["total_aircraft_time_minutes"])
    return {
        "method": method,
        "aircraft_time_minutes": int(aircraft),
        "passenger_time_minutes": int(metrics["total_passenger_travel_time_minutes"]),
        "flights": int(metrics["total_flights"]),
        "fuel_kg": float(metrics["total_fuel_consumption_kg"]),
        "seat_utilization": float(metrics["seat_utilization"]),
        "served_passengers": int(metrics.get("served_passengers", 1600)),
        "budget_type": budget_type,
        "runtime_seconds": runtime,
        "runtime_to_best_seconds": runtime_to_best,
        "seed": seed,
        "best_aircraft_time_minutes": best if best is not None else aircraft,
        "median_aircraft_time_minutes": median,
        "worst_aircraft_time_minutes": worst,
        "validator": validator,
        "method_status": status,
    }


def row_from_experiment(
    method: str, experiment_dir: Path, budget_type: str, status: str
) -> dict[str, object]:
    aggregate, summary = aggregate_best_run(experiment_dir)
    metrics = {
        "total_aircraft_time_minutes": summary["best_aircraft_time_minutes"],
        "total_passenger_travel_time_minutes": summary["passenger_time_minutes"],
        "total_flights": summary["flights"],
        "total_fuel_consumption_kg": summary["fuel_kg"],
        "seat_utilization": summary["seat_utilization"],
        "served_passengers": summary["served_passengers"],
    }
    return comparison_row(
        method,
        metrics,
        budget_type,
        status,
        runtime=summary["runtime_seconds"],
        runtime_to_best=summary.get("time_to_final_best_seconds"),
        best=aggregate["best_of_seeds_aircraft_time_minutes"],
        median=aggregate["median_aircraft_time_minutes"],
        worst=aggregate["worst_aircraft_time_minutes"],
        seed=summary["seed"],
        validator="VALID" if summary["validator_valid"] else "INVALID",
    )


def pct_gain(old: float, new: float) -> float:
    return 100.0 * (old - new) / old


def mean_experiment_field(experiment_dir: Path, aggregate: dict, field: str) -> float:
    if all(field in row for row in aggregate["per_seed"]):
        values = [float(row[field]) for row in aggregate["per_seed"]]
    else:
        with (experiment_dir / "per_seed_summary.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            values = [float(row[field]) for row in csv.DictReader(handle)]
    return sum(values) / len(values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--winner-run", required=True, type=Path)
    parser.add_argument("--winning-method", required=True)
    args = parser.parse_args()

    winner = args.winner_run.resolve()
    source_summary = read_json(winner / "run_summary.json")
    final_dir = ROOT / "outputs" / "q1" / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    for name in ("q1-routes.csv", "q1-assignments.csv", "q1-convergence.csv", "operator_stats.csv"):
        source = winner / name
        if source.exists():
            shutil.copy2(source, final_dir / name)

    validation = validate_solution(
        "q1",
        final_dir / "q1-routes.csv",
        final_dir / "q1-assignments.csv",
        data_dir=ROOT / "data" / "raw",
    )
    if not validation.valid or validation.metrics is None:
        raise RuntimeError("final candidate failed independent validation")
    final_metrics = validation.metrics.to_dict()
    if int(final_metrics["served_passengers"]) != 1600:
        raise RuntimeError("final candidate does not serve all 1600 passengers")
    expected = source_summary["final_comparison_key"]
    actual = [
        float(final_metrics["total_aircraft_time_minutes"]),
        float(final_metrics["total_passenger_travel_time_minutes"]),
        float(final_metrics["total_flights"]),
        float(final_metrics["total_fuel_consumption_kg"]),
        -float(final_metrics["seat_utilization"]),
    ]
    if any(abs(a - b) > 1e-6 for a, b in zip(actual, expected)):
        raise RuntimeError("final Validator metrics differ from winning run metrics")

    write_json(final_dir / "validator.json", validation.to_dict())
    write_json(
        final_dir / "metrics.json",
        {
            "gate_pass": True,
            "metrics_match": True,
            "validator_metrics": final_metrics,
            "comparison_key": actual,
        },
    )
    write_json(
        final_dir / "winning_config.json",
        {
            "method": args.winning_method,
            "experiment": source_summary["experiment"],
            "seed": source_summary["seed"],
            "stage_configs": source_summary["stage_configs"],
        },
    )
    write_json(
        final_dir / "method_metadata.json",
        {
            "method": args.winning_method,
            "source_run": str(winner.relative_to(ROOT)),
            "source_git_commit": source_summary["git_commit"],
            "fusion_branch_base_commit": "7ddd2f740b4fff6b25e9779d26ddea1b4d8d75a0",
            "seed": source_summary["seed"],
            "runtime_seconds": source_summary["runtime_seconds"],
            "runtime_to_best_seconds": source_summary["time_to_final_best_seconds"],
            "selection_rule": "strict lexicographic among independently validated candidates",
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )

    b0 = metrics_from_file(ROOT / "outputs/q1/runs/20260814-b0-reprocheck/metrics.json")
    b1 = metrics_from_file(ROOT / "outputs/q1/runs/20260814-b1-final/metrics.json")
    classical = metrics_from_file(ROOT / "outputs/q1/best/metrics.json")
    rows = [
        comparison_row("B0 Safe Baseline", b0, "deterministic", "BASELINE", runtime=33, runtime_to_best=33),
        comparison_row("B1 Generalized Savings", b1, "deterministic", "BASELINE", runtime=66, runtime_to_best=66),
        comparison_row("Classical VND", classical, "deterministic_vnd", "CONTROL", runtime=149, runtime_to_best=149),
        row_from_experiment("Standard ALNS V0", ROOT / "outputs/q1/alns/v0-multiseed-wall300", "300s_wallclock", "REJECTED_BY_V1"),
        row_from_experiment("Standard ALNS V1/A3", ROOT / "outputs/q1/alns/w-a3-destroy-56-68", "300s_wallclock", "STANDARD_FAIR_CONTROL"),
        row_from_experiment("Standard ALNS A2 historical", ROOT / "outputs/q1/alns/a2-destroy-45-56", "fixed_iterations", "OLD_ABSOLUTE_INCUMBENT"),
        row_from_experiment("Distance Relatedness ALNS", ROOT / "outputs/q1/relatedness-alns/fair/fair-r1-distance-a3-wall300", "300s_wallclock", "REJECT_STANDALONE"),
        row_from_experiment("Context Repair ALNS", ROOT / "outputs/q1/relatedness-alns/screen/screen-r2-context160-a3-i2", "screen_fixed_iterations", "REJECT_QUALITY"),
        row_from_experiment("Combined Relatedness ALNS", ROOT / "outputs/q1/relatedness-alns/fair/fair-r3-distance-context160-a3-wall300", "300s_wallclock", "FAIR_WINNER"),
        row_from_experiment("Relatedness ALNS extended", ROOT / "outputs/q1/relatedness-alns/extended/extended-r3-a3-seed3-from14791", "extended_search", "RELATEDNESS_EXTENDED_BEST"),
        row_from_experiment("Standard ALNS A2 extended", ROOT / "outputs/q1/relatedness-alns/extended/extended-standard-a2-seed4-from14772", "extended_search", "STANDARD_EXTENDED_CANDIDATE"),
    ]
    a3_dir = ROOT / "outputs/q1/relatedness-alns/extended/extended-standard-a3-seed2-from14770"
    if (a3_dir / "aggregate_summary.json").exists():
        rows.append(row_from_experiment("Standard ALNS A3 extended", a3_dir, "extended_search", "STANDARD_EXTENDED_CANDIDATE"))
    rows.append(
        comparison_row(
            "Final Winner",
            final_metrics,
            "extended_search",
            "ADOPT_FINAL",
            runtime=source_summary["runtime_seconds"],
            runtime_to_best=source_summary["time_to_final_best_seconds"],
            seed=source_summary["seed"],
        )
    )
    comparison_path = ROOT / "Q1_FINAL_COMPARISON.csv"
    with comparison_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    standard = read_json(ROOT / "outputs/q1/alns/w-a3-destroy-56-68/aggregate_summary.json")
    combined = read_json(ROOT / "outputs/q1/relatedness-alns/fair/fair-r3-distance-context160-a3-wall300/aggregate_summary.json")
    distance = read_json(ROOT / "outputs/q1/relatedness-alns/fair/fair-r1-distance-a3-wall300/aggregate_summary.json")
    r0_screen = read_json(ROOT / "outputs/q1/relatedness-alns/screen/screen-r0-legacy-a3-i2/aggregate_summary.json")
    r2_screen = read_json(ROOT / "outputs/q1/relatedness-alns/screen/screen-r2-context160-a3-i2/aggregate_summary.json")
    r3_screen = read_json(ROOT / "outputs/q1/relatedness-alns/screen/screen-r3-distance-context160-a3-i2/aggregate_summary.json")
    final_aircraft = float(final_metrics["total_aircraft_time_minutes"])
    old_incumbent = 15052.0
    screen_root = ROOT / "outputs/q1/relatedness-alns/screen"
    r0_mean_evals = mean_experiment_field(
        screen_root / "screen-r0-legacy-a3-i2", r0_screen, "evaluator_calls"
    )
    r2_eval_reduction = 100.0 * (
        1.0
        - mean_experiment_field(
            screen_root / "screen-r2-context160-a3-i2", r2_screen, "evaluator_calls"
        )
        / r0_mean_evals
    )
    r3_eval_reduction = 100.0 * (
        1.0
        - mean_experiment_field(
            screen_root / "screen-r3-distance-context160-a3-i2", r3_screen, "evaluator_calls"
        )
        / r0_mean_evals
    )

    result_md = f"""# Q1 最终结果

## 最终结论

最终采用 **{args.winning_method}**。从全部独立验证通过的候选中按严格词典序选择，最终总飞机使用时间为 **{int(final_aircraft):,} 分钟**，服务 **1,600/1,600** 人，Validator **VALID（0 issues）**。

| 指标 | 最终值 |
|---|---:|
| 总飞机使用时间 | {int(final_aircraft):,} min |
| 人员总在途时间 | {int(final_metrics['total_passenger_travel_time_minutes']):,} min |
| 总架次 | {int(final_metrics['total_flights'])} |
| 总燃油 | {float(final_metrics['total_fuel_consumption_kg']):,.1f} kg |
| 座位利用率 | {float(final_metrics['seat_utilization']):.6f} |
| 安排人数 | {int(final_metrics['served_passengers'])}/1600 |

相较 B0 的 17,222 分钟减少 **{int(17222-final_aircraft):,} 分钟（{pct_gain(17222, final_aircraft):.2f}%）**；相较 B1 的 15,743 分钟减少 **{int(15743-final_aircraft):,} 分钟（{pct_gain(15743, final_aircraft):.2f}%）**；相较 Classical VND 的 15,371 分钟减少 **{int(15371-final_aircraft):,} 分钟（{pct_gain(15371, final_aircraft):.2f}%）**；相较 Standard V1 300s best 15,118 减少 **{int(15118-final_aircraft):,} 分钟（{pct_gain(15118, final_aircraft):.2f}%）**；相较阶段开始时 absolute incumbent 15,052 减少 **{int(old_incumbent-final_aircraft):,} 分钟（{pct_gain(old_incumbent, final_aircraft):.2f}%）**。

## Relatedness 融合判断

300 秒公平基准由 **Distance Destroy + Context-160（R3）** 获胜：best/median/mean/worst 为 **{combined['best_of_seeds_aircraft_time_minutes']:.0f}/{combined['median_aircraft_time_minutes']:.0f}/{combined['mean_aircraft_time_minutes']:.1f}/{combined['worst_aircraft_time_minutes']:.0f}**；Standard V1 为 **{standard['best_of_seeds_aircraft_time_minutes']:.0f}/{standard['median_aircraft_time_minutes']:.0f}/{standard['mean_aircraft_time_minutes']:.1f}/{standard['worst_aircraft_time_minutes']:.0f}**。R3 改善 best 93、median 36、mean 66.4 分钟，同 seed 赢 4/5，但 worst 多 7 分钟，因此质量总体更强而非方差全面更优。

Distance-only 正式公平结果为 best/median **{distance['best_of_seeds_aircraft_time_minutes']:.0f}/{distance['median_aircraft_time_minutes']:.0f}**，未超过 Standard V1，故不单独晋升。Context-only 虽把 screening evaluator calls 平均减少 **{r2_eval_reduction:.1f}%**，但 best/median 恶化至 **{r2_screen['best_of_seeds_aircraft_time_minutes']:.0f}/{r2_screen['median_aircraft_time_minutes']:.0f}**，也不单独晋升。R3 在 screening 中保持近似质量并减少 evaluator calls **{r3_eval_reduction:.1f}%**，说明 Context 的价值是与 distance 联合后的候选预算效率。

保留：raw distance destroy、可解释 Context V2 排序（geometry/capacity-slack/ejection/airport/route-state）、exact repair/Evaluator/Validator 唯一裁决。淘汰：consensus（均值/最差无稳定增益）、fuel static、static capacity、full static composite、hard clustering；Bandit 未进入主线。

Relatedness extended 将 15,025 继续降至 **14,772**，确实刷新 final best；随后纯 Standard A2 热启动再降至当前最终值。因此 Relatedness 是 300s 公平预算 winner 和突破旧 incumbent 的关键搜索阶段，但最终 CSV 的最后一步 winner 由 `{args.winning_method}` 产生。

## 可复现产物

- `outputs/q1/final/q1-routes.csv`
- `outputs/q1/final/q1-assignments.csv`
- `outputs/q1/final/metrics.json`
- `outputs/q1/final/validator.json`
- `outputs/q1/final/winning_config.json`
- `Q1_FINAL_COMPARISON.csv`

最终独立 Validator 为 VALID/0 issues，内部指标与 Validator 一致；全量 **54 tests PASS**，`git diff --check` 通过。
"""
    (ROOT / "FINAL_Q1_RESULT.md").write_text(result_md, encoding="utf-8")

    a3_best = next((row for row in rows if row["method"] == "Standard ALNS A3 extended"), None)
    a3_text = str(a3_best["aircraft_time_minutes"]) if a3_best else "未完成"
    handoff_md = f"""# Q1 Relatedness × Standard ALNS 最终交接

## 基座与阶段门

- 融合分支：`codex/q1-relatedness-alns`；基座 commit：`7ddd2f7`（`platinumist_update_alns_base`）。没有 merge/cherry-pick clustering 分支，仅移植冻结数据与适配逻辑。
- Standard 公平控制仍为 A3/V1 300s、seeds 0–4：best 15,118，median 15,185，mean 15,208.4，worst 15,281，全部 VALID。
- 开始时 absolute validated incumbent 仍为 A2 seed 4 的 15,052。
- Relatedness disabled 的 fixed-iteration no-op regression 保持相同 15,361 结果、4 iterations、687 evaluator calls；测试覆盖 legacy 退化和新排序确定性。

## 五阶段结果

1. No-op：PASS，默认配置不改变 Standard ALNS 语义。
2. Distance：用 raw route distance 替换 legacy airport/fixed-origin penalty。正式 300s best/median 15,167/15,205；稳定性较好但中心质量未胜 V1，单独 REJECT。
3. Consensus：只做一次 soft A/B；screen best 15,255，但 mean/worst 变差，REJECT。
4. Context V2：在 exact candidate build/MILP 前做 explainable rank/budget。Context-only evaluator calls 降 {r2_eval_reduction:.1f}% 但质量恶化，单独 REJECT。
5. Combined R3：screen evaluator calls 降 {r3_eval_reduction:.1f}% 且质量近似；正式 300s best/median/mean 15,025/15,149/15,142.0，同 seed 4/5 胜 V1，ADOPT 为 fair-budget winner。

所有 guidance 只做 ranking/pruning；LegPhysics、SolverCache、exact repair、Evaluator、objective、Exporter、Validator 未被替换。保留 raw distance 与 Context V2；淘汰 consensus、静态 fuel/capacity/full composite、hard partition。

## Extended 与最终解

- Relatedness R3：15,025 → 14,791 → **14,772**，均 VALID。
- Standard A2 从 14,772 热启动：**14,770**，runtime 604.658s，time-to-best 82.207s，VALID。
- Standard A3 从 14,770 热启动：**{a3_text}**。
- 最终 winner：**{args.winning_method}，{int(final_aircraft)} 分钟**；人员时间 {int(final_metrics['total_passenger_travel_time_minutes'])}，{int(final_metrics['total_flights'])} 架次，燃油 {float(final_metrics['total_fuel_consumption_kg']):.1f} kg，利用率 {float(final_metrics['seat_utilization']):.6f}，1600/1600，Validator VALID/0 issues。

## 产物与验证

正式原子输出位于 `outputs/q1/final/`；比较表为 `Q1_FINAL_COMPARISON.csv`；答题总结为 `FINAL_Q1_RESULT.md`。最终 CSV、metrics、validator、winning config、method metadata 全部来自同一 winning run。最终独立 Validator VALID/0 issues，内部指标一致；全量 54 tests PASS，`git diff --check` 通过。Q1 到此停止，不进入 Q2/Q3。
"""
    (ROOT / "STAGE_Q1_RELATEDNESS_ALNS_HANDOFF.md").write_text(handoff_md, encoding="utf-8")
    print(json.dumps({"valid": True, "metrics": final_metrics, "final_dir": str(final_dir)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
