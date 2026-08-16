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


def chinese_font(font_path: Path | None) -> tuple[font_manager.FontProperties, bool]:
    if font_path:
        return font_manager.FontProperties(fname=str(font_path)), True
    for family in ("Noto Sans CJK SC", "Microsoft YaHei", "SimHei"):
        try:
            font_manager.findfont(family, fallback_to_default=False)
            return font_manager.FontProperties(family=family), True
        except ValueError:
            continue
    return font_manager.FontProperties(family="DejaVu Sans"), False


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制Q1与Q2融合优化阶段图")
    parser.add_argument("--q1", type=Path, required=True)
    parser.add_argument("--q2", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    chinese, has_chinese = chinese_font(args.font)
    plt.rcParams["axes.unicode_minus"] = False

    q1_all = rows(args.q1)
    q1_lookup = {row.get("stage", row.get("method", "")): row for row in q1_all}
    q1_names = ["Control", "Round 1", "Round 2", "Round 4", "Education"]
    q1_labels = (["标准ALNS控制", "首轮主问题", "88架次候选", "反馈后主问题", "标准ALNS教育"]
                 if has_chinese else ["ALNS control", "Master R1", "88-flight", "Master R4", "ALNS education"])
    q1_values = [int(q1_lookup[name]["aircraft_time_minutes"]) for name in q1_names]
    fig, ax = plt.subplots(figsize=(9.2, 4.8))
    ax.plot(range(len(q1_values)), q1_values, color="#2878b5", marker="o", linewidth=2.2)
    ax.axhline(13337, color="#6b7280", linestyle="--", linewidth=1.2,
               label="理论下界 13337 min" if has_chinese else "Global lower bound: 13337 min")
    for index, value in enumerate(q1_values):
        ax.annotate(str(value), (index, value), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=8)
    ax.set_xticks(range(len(q1_labels)), q1_labels, fontproperties=chinese, rotation=18)
    ax.set_ylabel("总飞机使用时间/min" if has_chinese else "Aircraft time / min", fontproperties=chinese)
    ax.set_title("问题一：精英路线池主问题与ALNS教育的改进链" if has_chinese
                 else "Q1: elite route-pool master and ALNS education", fontproperties=chinese)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(prop=chinese)
    save_both(fig, args.output_dir / "q1_integrated_improvement")

    q2_all = rows(args.q2)
    q2_pick = ["canonical-rmp", "standard-alns", "round1", "round2", "round3-final"]
    q2_labels = (["候选RMP", "标准ALNS", "第一轮", "第二轮", "第三轮最终"]
                 if has_chinese else ["RMP", "Standard ALNS", "Round 1", "Round 2", "Round 3"])
    q2_lookup = {row["solution"]: row for row in q2_all}
    q2_values = [int(q2_lookup[name]["aircraft_time"]) for name in q2_pick]
    q2_flights = [int(q2_lookup[name]["flights"]) for name in q2_pick]
    fig, left = plt.subplots(figsize=(9.2, 4.8))
    right = left.twinx()
    left.plot(range(len(q2_values)), q2_values, color="#2878b5", marker="o", linewidth=2.2,
              label="飞机时间" if has_chinese else "Aircraft time")
    right.step(range(len(q2_flights)), q2_flights, where="mid", color="#d97706", linewidth=2.0,
               label="架次数" if has_chinese else "Flights")
    for index, value in enumerate(q2_values):
        left.annotate(str(value), (index, value), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=8)
    left.set_xticks(range(len(q2_labels)), q2_labels, fontproperties=chinese, rotation=18)
    left.set_ylabel("总飞机使用时间/min" if has_chinese else "Aircraft time / min", fontproperties=chinese)
    right.set_ylabel("架次数" if has_chinese else "Flights", fontproperties=chinese)
    left.set_title("问题二：精确局部重构、重启与吸收式强化" if has_chinese
                   else "Q2: exact local repair, restarts and absorption", fontproperties=chinese)
    left.grid(axis="y", alpha=0.25)
    handles = left.get_lines() + right.get_lines()
    left.legend(handles, [item.get_label() for item in handles], prop=chinese, loc="upper right")
    save_both(fig, args.output_dir / "q2_round3_improvement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
