from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import font_manager

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_csv


FIELDS = [
    "stage",
    "global_iteration",
    "iteration",
    "elapsed_seconds",
    "operator",
    "destroyed_routes",
    "accepted",
    "improved_current",
    "new_global_best",
    "current_aircraft_time_minutes",
    "best_aircraft_time_minutes",
    "current_passenger_time_minutes",
    "best_passenger_time_minutes",
    "current_flights",
    "best_flights",
    "temperature",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="合并并绘制问题一多阶段ALNS收敛曲线")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--csv-output", required=True, type=Path)
    parser.add_argument("--figure-output", required=True, type=Path)
    parser.add_argument("--lower-bound", type=float, default=13337.0)
    parser.add_argument("--font", type=Path, help="中文字体文件（TTF/OTF/TTC）")
    args = parser.parse_args()

    if args.font is not None:
        chinese_font = font_manager.FontProperties(fname=str(args.font))
    else:
        chinese_font = font_manager.FontProperties(
            family=["Noto Sans CJK SC", "Source Han Sans CN", "Microsoft YaHei", "SimHei"]
        )

    merged: list[dict[str, object]] = []
    elapsed_offset = 0.0
    global_iteration = 0
    boundaries: list[int] = []
    for stage, path in enumerate(args.inputs, start=1):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            global_iteration += 1
            merged.append(
                {
                    "stage": stage,
                    "global_iteration": global_iteration,
                    **row,
                    "elapsed_seconds": round(elapsed_offset + float(row["elapsed_seconds"]), 6),
                }
            )
        if merged:
            elapsed_offset = float(merged[-1]["elapsed_seconds"])
            boundaries.append(global_iteration)
    write_csv(args.csv_output, FIELDS, merged)

    x = [int(row["global_iteration"]) for row in merged]
    best_time = [int(row["best_aircraft_time_minutes"]) for row in merged]
    best_flights = [int(row["best_flights"]) for row in merged]
    figure, axes = plt.subplots(
        2,
        1,
        figsize=(8.2, 5.8),
        sharex=True,
        gridspec_kw={"height_ratios": [2.1, 1.0]},
    )
    axes[0].step(
        x, best_time, where="post", linewidth=1.8,
        label="历史最优飞机时间",
    )
    axes[0].axhline(
        args.lower_bound, color="0.45", linestyle="--", linewidth=1.1,
        label="理论下界",
    )
    axes[0].set_ylabel("总飞机使用时间/min", fontproperties=chinese_font)
    axes[0].grid(alpha=0.25)
    axes[0].legend(frameon=False, ncol=2, prop=chinese_font)
    axes[1].step(x, best_flights, where="post", linewidth=1.6, color="#d97706")
    axes[1].set_xlabel("ALNS全局迭代次数", fontproperties=chinese_font)
    axes[1].set_ylabel("架次数", fontproperties=chinese_font)
    axes[1].grid(alpha=0.25)
    for boundary in boundaries[:-1]:
        for axis in axes:
            axis.axvline(boundary + 0.5, color="0.55", linestyle=":", linewidth=1.0)
        axes[0].text(
            boundary / 2,
            0.96,
            "阶段一：扩大搜索",
            transform=axes[0].get_xaxis_transform(),
            ha="center",
            va="top",
            fontproperties=chinese_font,
            color="0.35",
        )
        axes[0].text(
            (boundary + x[-1]) / 2,
            0.96,
            "阶段二：强化搜索",
            transform=axes[0].get_xaxis_transform(),
            ha="center",
            va="top",
            fontproperties=chinese_font,
            color="0.35",
        )
    axes[0].annotate(
        f"最终值：{best_time[-1]:,} min",
        xy=(x[-1], best_time[-1]),
        xytext=(-78, 14),
        textcoords="offset points",
        arrowprops={"arrowstyle": "->", "linewidth": 0.8},
        fontproperties=chinese_font,
    )
    figure.tight_layout()
    args.figure_output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.figure_output, bbox_inches="tight")
    plt.close(figure)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
