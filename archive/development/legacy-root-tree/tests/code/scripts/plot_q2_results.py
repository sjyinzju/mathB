from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import font_manager

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_csv
from src.solver import load_problem_data, load_q2_solution
from src.solver.evaluator import evaluate_route
from src.solver.q2 import q2_direction


def _metrics(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("validator_metrics") or data.get("joint_internal_metrics")


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制问题二方法对比和代表性混合架次状态图")
    parser.add_argument("--separate-metrics", required=True, type=Path)
    parser.add_argument("--minimal-metrics", required=True, type=Path)
    parser.add_argument("--final-metrics", required=True, type=Path)
    parser.add_argument("--routes", required=True, type=Path)
    parser.add_argument("--assignments", required=True, type=Path)
    parser.add_argument("--comparison-output", required=True, type=Path)
    parser.add_argument("--route-output", required=True, type=Path)
    parser.add_argument("--state-csv", required=True, type=Path)
    parser.add_argument("--progression-csv", required=True, type=Path)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()

    if args.font:
        chinese_font = font_manager.FontProperties(fname=str(args.font))
    else:
        chinese_font = font_manager.FontProperties(
            family=["Noto Sans CJK SC", "Source Han Sans CN", "Microsoft YaHei", "SimHei"]
        )

    stages = [
        ("三类分开运输", _metrics(args.separate_metrics)),
        ("最小联合候选池", _metrics(args.minimal_metrics)),
        ("扩展联合候选池", _metrics(args.final_metrics)),
    ]
    progression = [
        {
            "stage": name,
            "aircraft_time_minutes": int(values["total_aircraft_time_minutes"]),
            "passenger_time_minutes": int(values["total_passenger_travel_time_minutes"]),
            "flights": int(values["total_flights"]),
            "fuel_kg": float(values["total_fuel_consumption_kg"]),
            "seat_utilization_percent": 100.0 * float(values["seat_utilization"]),
        }
        for name, values in stages
    ]
    write_csv(
        args.progression_csv,
        [
            "stage",
            "aircraft_time_minutes",
            "passenger_time_minutes",
            "flights",
            "fuel_kg",
            "seat_utilization_percent",
        ],
        progression,
    )

    names = [row["stage"] for row in progression]
    figure, axes = plt.subplots(1, 3, figsize=(10.2, 3.6))
    panels = (
        ("aircraft_time_minutes", "总飞机使用时间/min", "#2878b5"),
        ("flights", "架次数", "#d97706"),
        ("seat_utilization_percent", "座位利用率/%", "#3b8d5a"),
    )
    for axis, (key, ylabel, color) in zip(axes, panels):
        values = [row[key] for row in progression]
        bars = axis.bar(range(3), values, color=color, alpha=0.88, width=0.62)
        axis.set_xticks(range(3), names, fontproperties=chinese_font, rotation=12)
        axis.set_ylabel(ylabel, fontproperties=chinese_font)
        axis.grid(axis="y", alpha=0.22)
        axis.spines[["top", "right"]].set_visible(False)
        for bar, value in zip(bars, values):
            label = f"{value:,.1f}" if isinstance(value, float) else f"{value:,}"
            axis.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height(),
                label,
                ha="center",
                va="bottom",
                fontsize=8.5,
            )
    figure.tight_layout()
    args.comparison_output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.comparison_output, bbox_inches="tight")
    plt.close(figure)

    data = load_problem_data()
    solution = load_q2_solution(args.routes, args.assignments, data)
    ranked = []
    for index, route in enumerate(solution.routes):
        directions = {
            q2_direction(item.origin_id, item.destination_id, data.config.airports)
            for item in route.assignments
        }
        evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
        ranked.append(
            (
                len(directions),
                len(route.assignments),
                len(route.stops),
                evaluation.seat_utilization,
                -index,
                route,
                evaluation,
            )
        )
    *_, route, evaluation = max(ranked, key=lambda item: item[:-2])
    aircraft = data.config.aircraft_types[route.aircraft_type]
    state_rows = []
    for index, leg in enumerate(evaluation.legs):
        destination_stop = route.stops[index + 1]
        state_rows.append(
            {
                "leg_order": index,
                "leg": f"{leg.origin}→{leg.destination}",
                "departure_load": leg.departure_load,
                "capacity": aircraft.seats,
                "arrival_fuel_kg": leg.arrival_fuel_kg,
                "departure_fuel_kg": leg.departure_fuel_kg,
                "reserve_kg": aircraft.reserve_kg,
                "refuel_at_destination": int(destination_stop.refuel),
            }
        )
    write_csv(
        args.state_csv,
        [
            "leg_order",
            "leg",
            "departure_load",
            "capacity",
            "arrival_fuel_kg",
            "departure_fuel_kg",
            "reserve_kg",
            "refuel_at_destination",
        ],
        state_rows,
    )

    x = list(range(len(state_rows)))
    labels = [row["leg"] for row in state_rows]
    figure, left = plt.subplots(figsize=(9.2, 4.4))
    bars = left.bar(
        x,
        [row["departure_load"] for row in state_rows],
        color="#2878b5",
        alpha=0.82,
        label="航段载客量",
    )
    left.axhline(aircraft.seats, color="#2878b5", linestyle="--", linewidth=1.1)
    left.set_ylabel("离站载客量/人", fontproperties=chinese_font)
    left.set_xticks(x, labels, fontproperties=chinese_font, rotation=20)
    left.grid(axis="y", alpha=0.22)
    right = left.twinx()
    right.plot(
        x,
        [row["arrival_fuel_kg"] for row in state_rows],
        color="#d97706",
        marker="o",
        linewidth=1.8,
        label="到达余油",
    )
    right.axhline(
        aircraft.reserve_kg,
        color="#b91c1c",
        linestyle=":",
        linewidth=1.2,
        label="安全余油",
    )
    right.set_ylabel("燃油量/kg", fontproperties=chinese_font)
    for bar, row in zip(bars, state_rows):
        left.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            str(row["departure_load"]),
            ha="center",
            va="bottom",
            fontsize=8.5,
        )
        if row["refuel_at_destination"]:
            right.annotate(
                "加油",
                (row["leg_order"], row["arrival_fuel_kg"]),
                xytext=(0, -18),
                textcoords="offset points",
                ha="center",
                fontproperties=chinese_font,
                color="#9a5b05",
            )
    handles_left, labels_left = left.get_legend_handles_labels()
    handles_right, labels_right = right.get_legend_handles_labels()
    left.legend(
        handles_left + handles_right,
        labels_left + labels_right,
        prop=chinese_font,
        frameon=False,
        ncol=3,
        loc="upper center",
    )
    figure.tight_layout()
    args.route_output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.route_output, bbox_inches="tight")
    plt.close(figure)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
