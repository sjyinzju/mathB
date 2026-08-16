from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager, patches

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data_pipeline import TIME_FORMAT
from src.io_utils import write_csv
from src.solver import load_problem_data


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _minutes(value: str, origin: datetime) -> int:
    return round((datetime.strptime(value, TIME_FORMAT) - origin).total_seconds() / 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制问题三排班、裕度和机队利用率图")
    parser.add_argument("--routes", type=Path, required=True)
    parser.add_argument("--assignments", type=Path, required=True)
    parser.add_argument("--people", type=Path, default=ROOT / "data/raw/peopleQ3.csv")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    data = load_problem_data()
    origin = data.config.planning_start
    chinese = (
        font_manager.FontProperties(fname=str(args.font))
        if args.font
        else font_manager.FontProperties(
            family=["Noto Sans CJK SC", "Source Han Sans CN", "Microsoft YaHei", "SimHei"]
        )
    )
    plt.rcParams["axes.unicode_minus"] = False

    route_rows = _rows(args.routes)
    assignment_rows = _rows(args.assignments)
    people = {row["person_id"]: row for row in _rows(args.people)}
    stops: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in route_rows:
        stops[(row["aircraft_id"], int(row["flight_no"]))].append(row)
    flights = []
    for key, values in stops.items():
        values.sort(key=lambda row: int(row["stop_order"]))
        start = _minutes(values[0]["departure_time"], origin)
        end = _minutes(values[-1]["arrival_time"], origin)
        flights.append((key[0], key[1], start, end, values))
    flights.sort(key=lambda item: (item[0], item[2]))

    aircraft_ids = list(data.config.fleet_ids)
    colors = {"T1": "#2878b5", "T2": "#d97706", "T3": "#3b8d5a"}
    figure, axis = plt.subplots(figsize=(11.2, 7.2))
    ymap = {aircraft: index for index, aircraft in enumerate(aircraft_ids)}
    for aircraft, _flight_no, start, end, _ in flights:
        aircraft_type = aircraft.split("-")[1]
        axis.broken_barh(
            [(start / 1440.0, (end - start) / 1440.0)],
            (ymap[aircraft] - 0.34, 0.68),
            facecolors=colors[aircraft_type],
            edgecolors="white",
            linewidth=0.25,
        )
    for day in range(8):
        axis.axvline(day, color="#6b7280", lw=0.55, alpha=0.45)
    axis.set_xlim(0, 7)
    axis.set_xticks(np.arange(0.5, 7.5), [f"8月{day}日" for day in range(3, 10)], fontproperties=chinese)
    axis.set_yticks(range(len(aircraft_ids)), aircraft_ids, fontsize=7.2)
    axis.set_xlabel("规划日期", fontproperties=chinese)
    axis.set_ylabel("具体飞机（机场--机型--编号）", fontproperties=chinese)
    axis.set_title("24架直升机七日排班甘特图", fontproperties=chinese)
    axis.grid(axis="y", alpha=0.12)
    axis.invert_yaxis()
    axis.legend(
        handles=[patches.Patch(color=color, label=f"机型{kind}") for kind, color in colors.items()],
        prop=chinese,
        ncol=3,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.08),
        frameon=False,
    )
    figure.tight_layout()
    for suffix in ("pdf", "png"):
        figure.savefig(args.output_dir / f"q3_gantt.{suffix}", dpi=220, bbox_inches="tight")
    plt.close(figure)

    flight_lookup = {(a, str(n)): values for a, n, _, _, values in flights}
    margin_rows = []
    labels = {
        "emergency": "应急处置",
        "production": "增储上产",
        "shift": "常规倒班",
        "temporary": "临时任务",
    }
    by_task: dict[str, list[int]] = defaultdict(list)
    for row in assignment_rows:
        if not row["aircraft_id"]:
            continue
        person = people[row["person_id"]]
        delivery = int(row["delivery_stop_order"])
        route = flight_lookup[(row["aircraft_id"], row["flight_no"])]
        arrival = _minutes(route[delivery]["arrival_time"], origin)
        latest = _minutes(person["latest_arrival_time"], origin)
        margin = latest - arrival
        by_task[person["task_type"]].append(margin)
        margin_rows.append(
            {
                "person_id": row["person_id"],
                "task_type": person["task_type"],
                "arrival_margin_minutes": margin,
            }
        )
    write_csv(
        args.output_dir / "q3_time_window_margin.csv",
        ["person_id", "task_type", "arrival_margin_minutes"],
        margin_rows,
    )
    order = [kind for kind in ("emergency", "production", "shift", "temporary") if by_task[kind]]
    figure, axis = plt.subplots(figsize=(7.6, 4.5))
    box = axis.boxplot(
        [by_task[kind] for kind in order],
        tick_labels=[labels[kind] for kind in order],
        showfliers=False,
        patch_artist=True,
    )
    for patch, color in zip(box["boxes"], ("#c2413b", "#d97706", "#2878b5", "#7c3aed")):
        patch.set_facecolor(color)
        patch.set_alpha(0.78)
    for label in axis.get_xticklabels():
        label.set_fontproperties(chinese)
    axis.axhline(0, color="#b91c1c", linestyle="--", lw=1)
    axis.set_ylabel("最晚到达时刻减实际到达时刻/min", fontproperties=chinese)
    axis.set_title("已服务人员送达时间裕度分布", fontproperties=chinese)
    axis.grid(axis="y", alpha=0.22)
    figure.tight_layout()
    for suffix in ("pdf", "png"):
        figure.savefig(args.output_dir / f"q3_time_window_margin.{suffix}", dpi=220, bbox_inches="tight")
    plt.close(figure)

    utilization = np.zeros((len(aircraft_ids), 7))
    utilization_rows = []
    for aircraft, _flight_no, start, end, _ in flights:
        day = start // 1440
        utilization[ymap[aircraft], day] += end - start
    utilization_percent = 100.0 * utilization / 840.0
    for aircraft in aircraft_ids:
        for day in range(7):
            utilization_rows.append(
                {
                    "aircraft_id": aircraft,
                    "day": day,
                    "date": f"2026-08-{day + 3:02d}",
                    "aircraft_time_minutes": int(utilization[ymap[aircraft], day]),
                    "operating_window_utilization_percent": round(
                        utilization_percent[ymap[aircraft], day], 3
                    ),
                }
            )
    write_csv(
        args.output_dir / "q3_aircraft_utilization.csv",
        ["aircraft_id", "day", "date", "aircraft_time_minutes", "operating_window_utilization_percent"],
        utilization_rows,
    )
    figure, axis = plt.subplots(figsize=(9.0, 7.0))
    image = axis.imshow(utilization_percent, aspect="auto", cmap="YlGnBu", vmin=0)
    axis.set_xticks(range(7), [f"8月{day}日" for day in range(3, 10)], fontproperties=chinese)
    axis.set_yticks(range(len(aircraft_ids)), aircraft_ids, fontsize=7.2)
    axis.set_xlabel("日期", fontproperties=chinese)
    axis.set_ylabel("具体飞机", fontproperties=chinese)
    axis.set_title("具体飞机分日运营时窗利用率", fontproperties=chinese)
    bar = figure.colorbar(image, ax=axis, pad=0.02)
    bar.set_label("利用率/%", fontproperties=chinese)
    figure.tight_layout()
    for suffix in ("pdf", "png"):
        figure.savefig(args.output_dir / f"q3_aircraft_utilization.{suffix}", dpi=220, bbox_inches="tight")
    plt.close(figure)
    print(
        f"Q3 figures written: flights={len(flights)}, served={len(margin_rows)}, "
        f"minimum_margin={min(row['arrival_margin_minutes'] for row in margin_rows)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
