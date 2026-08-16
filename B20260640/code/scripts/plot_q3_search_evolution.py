"""绘制问题三搜索演进图（30546 -> 29659 -> 29155 -> 28868 -> 28728）。

数据来源（均为仓库内已验证产物，禁止手改）：
- 30546: 上一版深度搜索解（code/outputs/q3/runs/20260815-q3-v6-deep/metrics.json，173架次）
- 29659: Closure-P2（code/outputs/q3/closure_p2_best/metrics.json）
- 29155: PRO V1 / 参数筛选最优组（code/outputs/q3/best/metrics.json、parameter-screen.csv）
- 28868: Multi-Island Deep ALNS（deep-islands.csv、convergence.csv）
- 28728: Optional Rescue + Mandatory P0 Feedback（current_incumbent/metrics.json、
  optional-rescue-dossier-v2.json、final-feedback.json）
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import font_manager

STAGES = [
    ("旧深度搜索解\n(v6-deep)", 30546),
    ("Closure-P2\n重构", 29659),
    ("PRO V1\n精英池邻域", 29155),
    ("V2异构筛选\n+多岛深度ALNS", 28868),
    ("Optional Rescue\n+P0强制反馈", 28728),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="问题三搜索演进图")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    plt.rcParams["axes.unicode_minus"] = False
    chinese = font_manager.FontProperties(
        family=["Noto Sans CJK SC", "Source Han Sans CN", "Microsoft YaHei", "SimHei"]
    )

    labels = [label for label, _ in STAGES]
    values = [value for _, value in STAGES]

    figure, axis = plt.subplots(figsize=(8.6, 4.2))
    bars = axis.bar(range(len(values)), values, color="#3d6cb4", width=0.6)
    bars[-1].set_color("#c0392b")
    for idx, value in enumerate(values):
        axis.text(idx, value + 120, str(value), ha="center", fontsize=10)
        if idx > 0:
            delta = values[idx - 1] - value
            axis.annotate(
                f"-{delta}",
                xy=(idx - 0.5, (values[idx - 1] + value) / 2),
                ha="center",
                fontsize=9,
                color="#444444",
            )
    axis.set_xticks(range(len(labels)))
    axis.set_xticklabels(labels, fontproperties=chinese, fontsize=9)
    axis.set_ylabel("总飞机使用时间/min", fontproperties=chinese)
    axis.set_ylim(28000, 31000)
    axis.set_title("问题三算法演进的飞机时间改进链", fontproperties=chinese)
    for spine in ("top", "right"):
        axis.spines[spine].set_visible(False)
    figure.tight_layout()
    for suffix in ("pdf", "png"):
        figure.savefig(args.output_dir / f"q3_search_evolution.{suffix}", dpi=220, bbox_inches="tight")
    print("q3_search_evolution written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
