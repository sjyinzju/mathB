from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .config import ROOT, load_config
from .data_pipeline import TIME_FORMAT, default_paths
from .io_utils import read_csv, write_csv


DISPLAY_HEADERS_ZH = {
    "question": "题目",
    "rank": "排名",
    "origin": "起点",
    "destination": "终点",
    "direction": "运输方向",
    "demand_count": "需求人数",
    "demand_share": "需求占比",
    "cumulative_share": "累计需求占比",
    "person_ids": "人员ID列表",
    "facility": "海上设施",
    "inbound_people": "流入人数",
    "outbound_people": "流出人数",
    "net_flow": "净流量（流入-流出）",
    "total_touch": "总触达人数",
    "can_refuel": "是否可加油",
    "flexible_land_origin_count": "起点为LAND人数",
    "flexible_land_destination_count": "终点为LAND人数",
    "facility_to_facility_count": "设施间穿梭人数",
    "fixed_origin_A01_count": "固定起点A01人数",
    "fixed_destination_A01_count": "固定终点A01人数",
    "flexible_origin_nearest_A01_count": "LAND起点最近A01人数",
    "flexible_destination_nearest_A01_count": "LAND终点最近A01人数",
    "fixed_origin_A02_count": "固定起点A02人数",
    "fixed_destination_A02_count": "固定终点A02人数",
    "flexible_origin_nearest_A02_count": "LAND起点最近A02人数",
    "flexible_destination_nearest_A02_count": "LAND终点最近A02人数",
    "fixed_origin_A03_count": "固定起点A03人数",
    "fixed_destination_A03_count": "固定终点A03人数",
    "flexible_origin_nearest_A03_count": "LAND起点最近A03人数",
    "flexible_destination_nearest_A03_count": "LAND终点最近A03人数",
    "unique_od_count": "唯一OD数量",
    "top_1_share": "前1个OD占比",
    "top_5_share": "前5个OD占比",
    "top_10_share": "前10个OD占比",
    "top_20_share": "前20个OD占比",
    "od_hhi": "OD集中度HHI",
    "effective_od_count_inverse_hhi": "等效OD数量（HHI倒数）",
    "task_type": "任务类型",
    "count": "人数",
    "window_min": "时间窗最小值（分钟）",
    "window_p25": "时间窗25分位数（分钟）",
    "window_median": "时间窗中位数（分钟）",
    "window_mean": "时间窗均值（分钟）",
    "window_p75": "时间窗75分位数（分钟）",
    "window_max": "时间窗最大值（分钟）",
    "slack_min": "松弛时间最小值（分钟）",
    "slack_p25": "松弛时间25分位数（分钟）",
    "slack_median": "松弛时间中位数（分钟）",
    "slack_mean": "松弛时间均值（分钟）",
    "slack_p75": "松弛时间75分位数（分钟）",
    "slack_max": "松弛时间最大值（分钟）",
    "negative_slack_count": "负松弛人数",
    "slack_under_30_count": "松弛不足30分钟人数",
    "slack_under_60_count": "松弛不足60分钟人数",
    "date": "日期",
    "earliest_demand_count": "按最早接载时间统计人数",
    "timestamp_role": "时间字段",
    "hour": "小时",
    "person_id": "人员ID",
    "earliest": "最早接载时间",
    "latest": "最晚送达时间",
    "window_minutes": "时间窗宽度（分钟）",
    "technical_min_travel_minutes_lower_bound": "技术最短旅行时间下界（分钟）",
    "slack_minutes": "松弛时间（分钟）",
    "candidate_pair_count": "候选人员对数量",
    "overlapping_window_pair_count": "时间窗重叠人员对数量",
    "pairwise_overlap_rate": "两两时间窗重叠率",
    "common_intersection_minutes": "共同时间窗长度（分钟）",
    "aircraft_type": "机型",
    "airport": "机场",
    "facility_count": "设施数量",
    "direct_leg_reachable_count": "满油单段可达设施数",
    "direct_round_trip_reachable_count": "直接往返可达设施数",
    "round_trip_refuel_dependent_count": "直接往返不可达设施数",
    "closed_route_feasible_within_5_stops_count": "5次海上着陆内闭合可达设施数",
    "closed_route_feasible_without_refuel_count": "无需加油闭合可达设施数",
    "refuel_required_count": "闭合航线必须加油设施数",
    "minimum_1_stop_count": "最少1次海上着陆设施数",
    "minimum_2_stop_count": "最少2次海上着陆设施数",
    "minimum_3_stop_count": "最少3次海上着陆设施数",
    "minimum_4_stop_count": "最少4次海上着陆设施数",
    "minimum_5_stop_count": "最少5次海上着陆设施数",
    "unreachable_within_5_stops_count": "5次海上着陆内不可达设施数",
}

DISPLAY_VALUES_ZH = {
    "q1": "问题1",
    "q2": "问题2",
    "q3": "问题3",
    "outbound": "出海",
    "inbound": "海返",
    "shuttle": "穿梭",
    "emergency": "紧急任务",
    "production": "生产任务",
    "shift": "倒班任务",
    "temporary": "临时任务",
    "ALL": "全部任务",
    "earliest": "最早接载时间",
    "latest": "最晚送达时间",
}


def _write_display_csv(path: Path, rows: list[dict[str, object]]) -> None:
    """Write an EDA-facing CSV with Chinese headers while preserving values and order."""
    source_fields = list(rows[0])
    localized_rows = [
        {
            DISPLAY_HEADERS_ZH.get(field, field): DISPLAY_VALUES_ZH.get(str(row[field]), row[field])
            for field in source_fields
        }
        for row in rows
    ]
    write_csv(path, list(localized_rows[0]), localized_rows)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _descriptive_row(label: str, values: list[float]) -> dict[str, object]:
    return {
        "group": label,
        "count": len(values),
        "min": min(values),
        "p25": round(_percentile(values, 0.25), 3),
        "median": round(statistics.median(values), 3),
        "mean": round(statistics.mean(values), 3),
        "p75": round(_percentile(values, 0.75), 3),
        "max": max(values),
    }


def _od_summary(processed_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for question in ("q1", "q2"):
        od_rows = read_csv(processed_dir / f"od_{question}.csv")
        total = sum(int(row["demand_count"]) for row in od_rows)
        ranked = sorted(od_rows, key=lambda row: (-int(row["demand_count"]), row["origin"], row["destination"]))
        cumulative = 0
        for rank, row in enumerate(ranked, start=1):
            count = int(row["demand_count"])
            cumulative += count
            rows.append(
                {
                    "question": question,
                    "rank": rank,
                    "origin": row["origin"],
                    "destination": row["destination"],
                    "direction": row["direction"],
                    "demand_count": count,
                    "demand_share": count / total,
                    "cumulative_share": cumulative / total,
                    "person_ids": row["person_ids"],
                }
            )
    return rows


def _facility_flow(processed_dir: Path) -> list[dict[str, object]]:
    config = load_config()
    rows: list[dict[str, object]] = []
    for question in ("q1", "q2"):
        demands = read_csv(processed_dir / f"demands_{question}.csv")
        inbound = Counter()
        outbound = Counter()
        for demand in demands:
            origin = demand["origin"]
            destination = demand["destination"]
            if origin in config.facilities:
                outbound[origin] += 1
            if destination in config.facilities:
                inbound[destination] += 1
        for facility in config.facilities:
            rows.append(
                {
                    "question": question,
                    "facility": facility,
                    "inbound_people": inbound[facility],
                    "outbound_people": outbound[facility],
                    "net_flow": inbound[facility] - outbound[facility],
                    "total_touch": inbound[facility] + outbound[facility],
                    "can_refuel": int(facility in config.refuel_facilities),
                }
            )
    return rows


def _demand_endpoint_summary(processed_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for question in ("q1", "q2", "q3"):
        demands = read_csv(processed_dir / f"demands_{question}.csv")
        for direction in sorted({row["direction"] for row in demands}):
            group = [row for row in demands if row["direction"] == direction]
            summary: dict[str, object] = {
                "question": question,
                "direction": direction,
                "demand_count": len(group),
                "flexible_land_origin_count": sum(row["origin"] == "LAND" for row in group),
                "flexible_land_destination_count": sum(row["destination"] == "LAND" for row in group),
                "facility_to_facility_count": sum(
                    row["origin_type"] == "FACILITY" and row["destination_type"] == "FACILITY" for row in group
                ),
            }
            for airport in ("A01", "A02", "A03"):
                summary[f"fixed_origin_{airport}_count"] = sum(row["origin"] == airport for row in group)
                summary[f"fixed_destination_{airport}_count"] = sum(row["destination"] == airport for row in group)
                summary[f"flexible_origin_nearest_{airport}_count"] = sum(
                    row["origin"] == "LAND" and row["nearest_airport"] == airport for row in group
                )
                summary[f"flexible_destination_nearest_{airport}_count"] = sum(
                    row["destination"] == "LAND" and row["nearest_airport"] == airport for row in group
                )
            rows.append(summary)
    return rows


def _od_concentration(od_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for question in ("q1", "q2"):
        counts = [int(row["demand_count"]) for row in od_rows if row["question"] == question]
        total = sum(counts)
        shares = sorted((count / total for count in counts), reverse=True)
        hhi = sum(share**2 for share in shares)
        result.append(
            {
                "question": question,
                "demand_count": total,
                "unique_od_count": len(counts),
                "top_1_share": shares[0],
                "top_5_share": sum(shares[:5]),
                "top_10_share": sum(shares[:10]),
                "top_20_share": sum(shares[:20]),
                "od_hhi": hhi,
                "effective_od_count_inverse_hhi": 1 / hhi,
            }
        )
    return result


def _q3_time_tables(processed_dir: Path) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    demands = read_csv(processed_dir / "demands_q3.csv")
    by_type_windows: dict[str, list[float]] = defaultdict(list)
    by_type_slack: dict[str, list[float]] = defaultdict(list)
    daily: Counter[tuple[str, str]] = Counter()
    hourly: Counter[tuple[str, str, int]] = Counter()
    for row in demands:
        task = row["task_type"]
        window = float(row["window_minutes"])
        slack = float(row["slack_minutes"])
        by_type_windows[task].append(window)
        by_type_slack[task].append(slack)
        earliest = datetime.strptime(row["earliest"], TIME_FORMAT)
        latest = datetime.strptime(row["latest"], TIME_FORMAT)
        daily[(earliest.strftime("%Y-%m-%d"), task)] += 1
        hourly[("earliest", task, earliest.hour)] += 1
        hourly[("latest", task, latest.hour)] += 1
    all_windows = [float(row["window_minutes"]) for row in demands]
    all_slack = [float(row["slack_minutes"]) for row in demands]
    summary: list[dict[str, object]] = []
    for task in sorted(by_type_windows):
        window_row = _descriptive_row(task, by_type_windows[task])
        slack_row = _descriptive_row(task, by_type_slack[task])
        summary.append(
            {
                "task_type": task,
                "count": window_row["count"],
                "window_min": window_row["min"],
                "window_p25": window_row["p25"],
                "window_median": window_row["median"],
                "window_mean": window_row["mean"],
                "window_p75": window_row["p75"],
                "window_max": window_row["max"],
                "slack_min": slack_row["min"],
                "slack_p25": slack_row["p25"],
                "slack_median": slack_row["median"],
                "slack_mean": slack_row["mean"],
                "slack_p75": slack_row["p75"],
                "slack_max": slack_row["max"],
                "negative_slack_count": sum(value < 0 for value in by_type_slack[task]),
                "slack_under_30_count": sum(value < 30 for value in by_type_slack[task]),
                "slack_under_60_count": sum(value < 60 for value in by_type_slack[task]),
            }
        )
    total_window = _descriptive_row("ALL", all_windows)
    total_slack = _descriptive_row("ALL", all_slack)
    summary.append(
        {
            "task_type": "ALL",
            "count": len(demands),
            "window_min": total_window["min"],
            "window_p25": total_window["p25"],
            "window_median": total_window["median"],
            "window_mean": total_window["mean"],
            "window_p75": total_window["p75"],
            "window_max": total_window["max"],
            "slack_min": total_slack["min"],
            "slack_p25": total_slack["p25"],
            "slack_median": total_slack["median"],
            "slack_mean": total_slack["mean"],
            "slack_p75": total_slack["p75"],
            "slack_max": total_slack["max"],
            "negative_slack_count": sum(value < 0 for value in all_slack),
            "slack_under_30_count": sum(value < 30 for value in all_slack),
            "slack_under_60_count": sum(value < 60 for value in all_slack),
        }
    )
    daily_rows = [
        {"date": date, "task_type": task, "earliest_demand_count": count}
        for (date, task), count in sorted(daily.items())
    ]
    hourly_rows = [
        {"timestamp_role": role, "task_type": task, "hour": hour, "demand_count": count}
        for (role, task, hour), count in sorted(hourly.items())
    ]
    tight = sorted(demands, key=lambda row: (float(row["slack_minutes"]), int(row["priority"]), row["latest"]))
    tight_rows = [
        {
            "rank": index,
            "person_id": row["person_id"],
            "origin": row["origin"],
            "destination": row["destination"],
            "direction": row["direction"],
            "task_type": row["task_type"],
            "earliest": row["earliest"],
            "latest": row["latest"],
            "window_minutes": row["window_minutes"],
            "technical_min_travel_minutes_lower_bound": row["technical_min_travel_minutes_lower_bound"],
            "slack_minutes": row["slack_minutes"],
        }
        for index, row in enumerate(tight[:100], start=1)
    ]
    return summary, daily_rows, hourly_rows, tight_rows


def _q3_compatibility(processed_dir: Path) -> list[dict[str, object]]:
    rows = read_csv(processed_dir / "demands_q3.csv")
    grouped: dict[tuple[str, str], list[tuple[datetime, datetime]]] = defaultdict(list)
    for row in rows:
        grouped[(row["origin"], row["destination"])].append(
            (datetime.strptime(row["earliest"], TIME_FORMAT), datetime.strptime(row["latest"], TIME_FORMAT))
        )
    result: list[dict[str, object]] = []
    for (origin, destination), windows in sorted(grouped.items()):
        compatible_pairs = 0
        total_pairs = len(windows) * (len(windows) - 1) // 2
        for index, (start_a, end_a) in enumerate(windows):
            for start_b, end_b in windows[index + 1 :]:
                if max(start_a, start_b) <= min(end_a, end_b):
                    compatible_pairs += 1
        result.append(
            {
                "origin": origin,
                "destination": destination,
                "demand_count": len(windows),
                "candidate_pair_count": total_pairs,
                "overlapping_window_pair_count": compatible_pairs,
                "pairwise_overlap_rate": compatible_pairs / total_pairs if total_pairs else 1.0,
                "common_intersection_minutes": max(
                    0,
                    round((min(end for _, end in windows) - max(start for start, _ in windows)).total_seconds() / 60),
                ),
            }
        )
    return result


def _fuel_summary(processed_dir: Path) -> list[dict[str, object]]:
    rows = read_csv(processed_dir / "features" / "fuel_network.csv")
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["aircraft_type"], row["airport"])].append(row)
    result: list[dict[str, object]] = []
    for (aircraft_type, airport), group in sorted(grouped.items()):
        result.append(
            {
                "aircraft_type": aircraft_type,
                "airport": airport,
                "facility_count": len(group),
                "direct_leg_reachable_count": sum(int(row["full_tank_direct_leg_feasible"]) for row in group),
                "direct_round_trip_reachable_count": sum(int(row["full_tank_direct_round_trip_feasible"]) for row in group),
                "round_trip_refuel_dependent_count": sum(int(row["likely_refuel_dependent"]) for row in group),
            }
        )
    return result


def _closed_route_summary(processed_dir: Path) -> list[dict[str, object]]:
    rows = read_csv(processed_dir / "features" / "closed_route_reachability.csv")
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["aircraft_type"], row["airport"])].append(row)
    result: list[dict[str, object]] = []
    for (aircraft_type, airport), group in sorted(grouped.items()):
        stop_distribution = Counter(
            int(row["minimum_sea_stops_with_refuel_allowed"])
            for row in group
            if row["minimum_sea_stops_with_refuel_allowed"]
        )
        result.append(
            {
                "aircraft_type": aircraft_type,
                "airport": airport,
                "facility_count": len(group),
                "closed_route_feasible_within_5_stops_count": sum(
                    int(row["closed_route_feasible_within_5_stops"]) for row in group
                ),
                "closed_route_feasible_without_refuel_count": sum(
                    int(row["closed_route_feasible_without_refuel"]) for row in group
                ),
                "refuel_required_count": sum(int(row["refuel_required_for_closed_route"]) for row in group),
                "minimum_1_stop_count": stop_distribution[1],
                "minimum_2_stop_count": stop_distribution[2],
                "minimum_3_stop_count": stop_distribution[3],
                "minimum_4_stop_count": stop_distribution[4],
                "minimum_5_stop_count": stop_distribution[5],
                "unreachable_within_5_stops_count": sum(
                    1 - int(row["closed_route_feasible_within_5_stops"]) for row in group
                ),
            }
        )
    return result


def _write_figures(
    eda_dir: Path,
    od_rows: list[dict[str, object]],
    facility_rows: list[dict[str, object]],
    daily_rows: list[dict[str, object]],
    time_summary: list[dict[str, object]],
    fuel_rows: list[dict[str, object]],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure_dir = eda_dir / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    plt.style.use("seaborn-v0_8-whitegrid")
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    q2_direction = Counter()
    seen: set[tuple[str, int]] = set()
    for row in od_rows:
        if row["question"] == "q2":
            q2_direction[str(row["direction"])] += int(row["demand_count"])
    fig, ax = plt.subplots(figsize=(7, 4.2))
    directions = ["outbound", "inbound", "shuttle"]
    labels = ["出海", "海返", "穿梭"]
    values = [q2_direction[direction] for direction in directions]
    ax.bar(labels, values, color=["#2563EB", "#0F766E", "#D97706"])
    ax.set(title="问题2各运输方向需求人数", ylabel="人数")
    for index, value in enumerate(values):
        ax.text(index, value + 25, f"{value:,}", ha="center")
    fig.tight_layout()
    fig.savefig(figure_dir / "q2_direction_counts.png", dpi=180)
    plt.close(fig)

    q2_flows = [row for row in facility_rows if row["question"] == "q2"]
    q2_flows.sort(key=lambda row: abs(int(row["net_flow"])), reverse=True)
    top = q2_flows[:20]
    fig, ax = plt.subplots(figsize=(10, 5.5))
    colors = ["#2563EB" if int(row["net_flow"]) >= 0 else "#D97706" for row in top]
    ax.bar([str(row["facility"]) for row in top], [int(row["net_flow"]) for row in top], color=colors)
    ax.axhline(0, color="#1F2937", linewidth=0.8)
    ax.set(title="问题2净流量绝对值最大的20个海上设施", ylabel="净流量（流入－流出）")
    ax.tick_params(axis="x", rotation=55)
    fig.tight_layout()
    fig.savefig(figure_dir / "q2_facility_net_flow.png", dpi=180)
    plt.close(fig)

    daily_total = Counter()
    for row in daily_rows:
        daily_total[str(row["date"])] += int(row["earliest_demand_count"])
    fig, ax = plt.subplots(figsize=(9, 4.5))
    dates = sorted(daily_total)
    values = [daily_total[date] for date in dates]
    ax.plot(dates, values, marker="o", color="#0F766E", linewidth=2)
    ax.set(title="问题3每日最早接载需求人数", ylabel="人数", xlabel="日期")
    ax.tick_params(axis="x", rotation=35)
    for date, value in zip(dates, values):
        ax.annotate(str(value), (date, value), xytext=(0, 6), textcoords="offset points", ha="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(figure_dir / "q3_daily_pressure.png", dpi=180)
    plt.close(fig)

    task_rows = [row for row in time_summary if row["task_type"] != "ALL"]
    fig, ax = plt.subplots(figsize=(8, 4.8))
    tasks = [DISPLAY_VALUES_ZH.get(str(row["task_type"]), str(row["task_type"])) for row in task_rows]
    medians = [float(row["slack_median"]) / 60 for row in task_rows]
    ax.bar(tasks, medians, color=["#DC2626", "#EA580C", "#2563EB", "#6B7280"])
    ax.set(title="问题3各任务类型乐观松弛时间中位数", ylabel="小时")
    ax.tick_params(axis="x", rotation=20)
    fig.tight_layout()
    fig.savefig(figure_dir / "q3_slack_by_task.png", dpi=180)
    plt.close(fig)

    aircraft_types = ["T1", "T2", "T3"]
    airports = ["A01", "A02", "A03"]
    lookup = {(str(row["aircraft_type"]), str(row["airport"])): int(row["direct_round_trip_reachable_count"]) for row in fuel_rows}
    fig, ax = plt.subplots(figsize=(8, 4.8))
    width = 0.23
    x = list(range(len(airports)))
    for offset, aircraft_type in enumerate(aircraft_types):
        ax.bar(
            [value + (offset - 1) * width for value in x],
            [lookup[(aircraft_type, airport)] for airport in airports],
            width=width,
            label=aircraft_type,
        )
    ax.set_xticks(x, airports)
    ax.set(title="各机型从不同机场直接往返可达的设施数", ylabel="可达设施数（共52个）")
    ax.legend(title="机型")
    fig.tight_layout()
    fig.savefig(figure_dir / "fuel_direct_roundtrip_coverage.png", dpi=180)
    plt.close(fig)


def _quality_markdown(quality: dict[str, object]) -> str:
    distance = quality["distance"]
    people = quality["people"]
    triangle = distance["triangle_inequality"]
    return f"""# 数据质量报告

## 结论

四个官方输入文件全部通过 Stage 1 数据质量检查。`data/raw/manifest.json` 保存了从官方附件复制出的只读工作副本路径、大小与 SHA-256，重复运行时若副本被改动会直接失败。

## 距离矩阵

- 规模：{distance['row_count']} 行 × {distance['column_node_count']} 个节点列；
- 节点组成：{distance['airport_count']} 个机场 + {distance['facility_count']} 个海上设施；
- 行列节点顺序一致：{distance['row_column_node_order_identical']}；
- 对角线全 0：{distance['zero_diagonal']}；无缺失、无负距离、矩阵对称；
- 非对角距离范围：{distance['min_off_diagonal_km']:.0f}-{distance['max_off_diagonal_km']:.0f} km；
- 所有需求节点均存在于距离矩阵：{distance['all_demand_nodes_present']}。

三角不等式不满足按不同定义分别统计为：

- 至少存在一个更短中间节点的无序节点对：{triangle['unordered_pair_count']}；
- 至少存在一个更短中间节点的有向节点对：{triangle['directed_pair_count']}；
- 具体 `(i,j,k)` 违例三元组：{triangle['violating_triple_count']}。

这些现象只作报告，未执行 Floyd、metric closure 或任何距离替换。后续若经中间节点飞行，必须把中间节点写入真实路线并计算着陆、停靠、载荷和油量。

## 人员文件

| 文件 | 人数 | 唯一ID | 出海 | 海返 | 穿梭 | 缺失/非法 |
|---|---:|---:|---:|---:|---:|---:|
| Q1 | {people['q1']['row_count']} | {people['q1']['unique_person_count']} | {people['q1']['direction_counts'].get('outbound', 0)} | 0 | 0 | 0 |
| Q2 | {people['q2']['row_count']} | {people['q2']['unique_person_count']} | {people['q2']['direction_counts'].get('outbound', 0)} | {people['q2']['direction_counts'].get('inbound', 0)} | {people['q2']['direction_counts'].get('shuttle', 0)} | 0 |
| Q3 | {people['q3']['row_count']} | {people['q3']['unique_person_count']} | {people['q3']['direction_counts'].get('outbound', 0)} | {people['q3']['direction_counts'].get('inbound', 0)} | {people['q3']['direction_counts'].get('shuttle', 0)} | 0 |

Q1 是 Q2 出海需求的精确子集；Q2 与 Q3 的人员 ID、起点和终点完全一致。Q3 时间窗均满足 `earliest < latest` 且处于规划期内，任务类型全部合法。
"""


def _dataset_markdown(
    od_rows: list[dict[str, object]],
    facility_rows: list[dict[str, object]],
    time_rows: list[dict[str, object]],
    fuel_rows: list[dict[str, object]],
    closed_route_rows: list[dict[str, object]],
    refuel_hub_rows: list[dict[str, str]],
) -> str:
    q1 = [row for row in od_rows if row["question"] == "q1"]
    q2 = [row for row in od_rows if row["question"] == "q2"]
    q2_top10 = sum(int(row["demand_count"]) for row in q2[:10]) / 4000
    q2_flows = sorted(
        [row for row in facility_rows if row["question"] == "q2"],
        key=lambda row: int(row["total_touch"]),
        reverse=True,
    )[:10]
    all_time = next(row for row in time_rows if row["task_type"] == "ALL")
    fuel_lines = "\n".join(
        f"- {row['aircraft_type']} / {row['airport']}：直接往返覆盖 {row['direct_round_trip_reachable_count']}/52，需依赖加油或组合停靠 {row['round_trip_refuel_dependent_count']}/52。"
        for row in fuel_rows
    )
    closed_route_lines = "\n".join(
        f"- {row['aircraft_type']} / {row['airport']}：5 次海上着陆上限内闭合航线可达 "
        f"{row['closed_route_feasible_within_5_stops_count']}/52；其中必须利用加油点 "
        f"{row['refuel_required_count']}/52；最少 1/2/3/4/5 次海上着陆的设施数依次为 "
        f"{row['minimum_1_stop_count']}/{row['minimum_2_stop_count']}/{row['minimum_3_stop_count']}/"
        f"{row['minimum_4_stop_count']}/{row['minimum_5_stop_count']}。"
        for row in closed_route_rows
    )
    top_refuel_hubs = sorted(
        refuel_hub_rows,
        key=lambda row: (
            -int(row["two_stop_supported_target_count"]),
            row["aircraft_type"],
            row["airport"],
            row["refuel_facility"],
        ),
    )[:10]
    refuel_hub_text = "、".join(
        f"{row['aircraft_type']}/{row['airport']} 经 {row['refuel_facility']}="
        f"{row['two_stop_supported_target_count']} 个目标"
        for row in top_refuel_hubs
    )
    flow_text = "、".join(f"{row['facility']}({row['total_touch']})" for row in q2_flows)
    return f"""# 数据集结构与优化启示

## 需求结构

- Q1：1600 名出海人员，{len(q1)} 个唯一 OD；
- Q2/Q3：4000 人，由 1600 出海、1600 海返、800 穿梭构成，共 {len(q2)} 个唯一 OD；
- Q2 前 10 个 OD 占全部需求的 {q2_top10:.1%}，说明存在可利用的重复流量，但并非少数 OD 完全支配；
- Q2 总触达量最高的设施为：{flow_text}。

直接启示：搜索层可以使用 OD 计数降低重复计算，但 `demands_q*` 必须始终保留一人一行；Q2 应联合匹配出海和海返，穿梭必须保持成对取送先后关系。

## Q3 时间压力

- 全部时间窗中位数为 {float(all_time['window_median'])/60:.2f} 小时；
- 基于“最快机型、放松前置航程与等待”的乐观在途下界，最小 slack 为 {all_time['slack_min']} 分钟；
- slack < 30 分钟共 {all_time['slack_under_30_count']} 人，slack < 60 分钟共 {all_time['slack_under_60_count']} 人；
- 该 slack 是启发式下界，不是最终可行性证明，不据此删除任何需求。

直接启示：Q3 候选架次应先锚定 priority 高、latest 早、slack 小的需求，再用宽窗 shift 填充余量。

## 燃油网络

{fuel_lines}

“单段满油可达”和“同机场直接往返可行”已分别统计。

### 5 次海上着陆约束下的闭合航线可达性

{closed_route_lines}

闭合搜索状态显式记录当前位置、剩余燃油、是否访问目标、是否加过油与海上着陆次数；这里的可达只说明存在燃油可行闭合航线，不代表该航线对载客、时间窗或总成本最优。

两停方案支持目标数最高的 10 个“机型/基地/加油点”组合为：{refuel_hub_text}。完整 72 个组合见 `data/processed/features/refuel_hub_summary.csv`。

即使每一段单独都能从满油飞行，也不能证明累计路线燃油可行；Validator 必须沿路线累积油耗，只在指定设施加满。
"""


def run_eda(root: Path = ROOT) -> dict[str, int]:
    paths = default_paths(root)
    quality_path = paths.processed_dir / "data_quality.json"
    if not quality_path.exists():
        raise FileNotFoundError("Run scripts/01_prepare_data.py before EDA")
    import json

    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    paths.eda_dir.mkdir(parents=True, exist_ok=True)
    od_rows = _od_summary(paths.processed_dir)
    facility_rows = _facility_flow(paths.processed_dir)
    endpoint_rows = _demand_endpoint_summary(paths.processed_dir)
    concentration_rows = _od_concentration(od_rows)
    time_rows, daily_rows, hourly_rows, tight_rows = _q3_time_tables(paths.processed_dir)
    compatibility_rows = _q3_compatibility(paths.processed_dir)
    fuel_rows = _fuel_summary(paths.processed_dir)
    closed_route_rows = _closed_route_summary(paths.processed_dir)
    refuel_hub_rows = read_csv(paths.processed_dir / "features" / "refuel_hub_summary.csv")

    _write_display_csv(paths.eda_dir / "od_summary.csv", od_rows)
    _write_display_csv(paths.eda_dir / "facility_flow.csv", facility_rows)
    _write_display_csv(paths.eda_dir / "demand_endpoint_summary.csv", endpoint_rows)
    _write_display_csv(paths.eda_dir / "od_concentration.csv", concentration_rows)
    _write_display_csv(paths.eda_dir / "q3_time_window_summary.csv", time_rows)
    _write_display_csv(paths.eda_dir / "q3_daily_pressure.csv", daily_rows)
    _write_display_csv(paths.eda_dir / "q3_hourly_pressure.csv", hourly_rows)
    _write_display_csv(paths.eda_dir / "q3_tightest_demands.csv", tight_rows)
    _write_display_csv(paths.eda_dir / "q3_compatibility_summary.csv", compatibility_rows)
    _write_display_csv(paths.eda_dir / "fuel_reachability_summary.csv", fuel_rows)
    _write_display_csv(paths.eda_dir / "closed_route_reachability_summary.csv", closed_route_rows)

    quality_md = _quality_markdown(quality)
    dataset_md = _dataset_markdown(
        od_rows,
        facility_rows,
        time_rows,
        fuel_rows,
        closed_route_rows,
        refuel_hub_rows,
    )
    (paths.eda_dir / "data_quality_report.md").write_text(quality_md, encoding="utf-8")
    (paths.eda_dir / "dataset_summary.md").write_text(dataset_md, encoding="utf-8")
    paper_text = """# 数据预处理与探索性分析

本阶段严格保留官方数据，建立节点、机型和个人需求三类规范表。`LAND` 被保留为三个候选机场的灵活端点，最近机场仅作为启发式特征。距离矩阵不满足三角不等式时未作最短路闭包；任意中间节点都必须作为真实海上停靠计入路线。

""" + quality_md.split("## 距离矩阵", 1)[1] + "\n\n" + dataset_md.split("## 需求结构", 1)[1]
    (root / "paper" / "01_data_analysis.md").write_text(paper_text, encoding="utf-8")
    _write_figures(paths.eda_dir, od_rows, facility_rows, daily_rows, time_rows, fuel_rows)
    return {
        "od_rows": len(od_rows),
        "facility_flow_rows": len(facility_rows),
        "demand_endpoint_summary_rows": len(endpoint_rows),
        "od_concentration_rows": len(concentration_rows),
        "q3_time_summary_rows": len(time_rows),
        "q3_compatibility_rows": len(compatibility_rows),
        "fuel_summary_rows": len(fuel_rows),
        "closed_route_summary_rows": len(closed_route_rows),
        "refuel_hub_rows": len(refuel_hub_rows),
        "figures": len(list((paths.eda_dir / "figures").glob("*.png"))),
    }
