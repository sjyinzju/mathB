from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import font_manager


def rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def save_both(figure: plt.Figure, stem: Path) -> None:
    figure.tight_layout()
    figure.savefig(stem.with_suffix(".pdf"), bbox_inches="tight")
    figure.savefig(stem.with_suffix(".png"), dpi=220, bbox_inches="tight")
    plt.close(figure)


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制Q1与Q2融合优化阶段图")
    parser.add_argument("--q1", type=Path, required=True)
    parser.add_argument("--q2", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    chinese = (
        font_manager.FontProperties(fname=str(args.font))
        if args.font
        else font_manager.FontProperties(family=["Noto Sans CJK SC", "Microsoft YaHei", "SimHei"])
    )
    plt.rcParams["axes.unicode_minus"] = False

    q1_lookup = {row["method"]: row for row in rows(args.q1)}
    q1_names = [
        "B0 Safe Baseline", "B1 Generalized Savings", "Classical VND",
        "Standard ALNS V1/A3", "Combined Relatedness ALNS",
        "Relatedness ALNS extended", "Final Winner",
    ]
    q1_labels = ["容量底解", "广义节约", "经典VND", "标准ALNS", "关联度ALNS", "关联度扩展", "最终解"]
    q1_values = [int(q1_lookup[name]["aircraft_time_minutes"]) for name in q1_names]
    fig, ax = plt.subplots(figsize=(9.2, 4.8))
    ax.plot(range(len(q1_values)), q1_values, color="#2878b5", marker="o", linewidth=2.2)
    ax.axhline(13337, color="#6b7280", linestyle="--", linewidth=1.2, label="理论下界 13337 min")
    for index, value in enumerate(q1_values):
        ax.annotate(str(value), (index, value), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=8)
    ax.set_xticks(range(len(q1_labels)), q1_labels, fontproperties=chinese, rotation=18)
    ax.set_ylabel("总飞机使用时间/min", fontproperties=chinese)
    ax.set_title("问题一：关联度引导与标准ALNS融合的改进链", fontproperties=chinese)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(prop=chinese)
    save_both(fig, args.output_dir / "q1_integrated_improvement")

    q2_all = rows(args.q2)
    q2_pick = [
        "round1_control", "stronger_elite_recombination", "global_elite_restart",
        "targeted_five", "cross_exchange", "ml_logging_best", "extended_finalist", "round2_final",
    ]
    q2_labels = ["第一轮", "精英重组", "全局重启", "五路线强化", "交叉交换", "日志组合", "决赛配置", "最终解"]
    q2_lookup = {row["stage"]: row for row in q2_all}
    q2_values = [int(q2_lookup[name]["aircraft_time_minutes"]) for name in q2_pick]
    q2_flights = [int(q2_lookup[name]["flights"]) for name in q2_pick]
    fig, left = plt.subplots(figsize=(9.2, 4.8))
    right = left.twinx()
    left.plot(range(len(q2_values)), q2_values, color="#2878b5", marker="o", linewidth=2.2, label="飞机时间")
    right.step(range(len(q2_flights)), q2_flights, where="mid", color="#d97706", linewidth=2.0, label="架次数")
    for index, value in enumerate(q2_values):
        left.annotate(str(value), (index, value), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=8)
    left.set_xticks(range(len(q2_labels)), q2_labels, fontproperties=chinese, rotation=18)
    left.set_ylabel("总飞机使用时间/min", fontproperties=chinese)
    right.set_ylabel("架次数", fontproperties=chinese)
    left.set_title("问题二：精确局部重构与多重启的改进链", fontproperties=chinese)
    left.grid(axis="y", alpha=0.25)
    handles = left.get_lines() + right.get_lines()
    left.legend(handles, [item.get_label() for item in handles], prop=chinese, loc="upper right")
    save_both(fig, args.output_dir / "q2_round2_improvement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
