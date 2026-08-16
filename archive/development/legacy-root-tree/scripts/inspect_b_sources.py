from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def extract_pdf(path: Path) -> dict[str, object]:
    reader = PdfReader(path)
    pages = [page.extract_text() or "" for page in reader.pages]
    return {"file": str(path), "page_count": len(pages), "pages": pages}


def summarize_distances(path: Path) -> dict[str, object]:
    rows = read_csv(path)
    nodes = list(rows[0].keys())[1:]
    matrix = {row["from_id"]: {node: float(row[node]) for node in nodes} for row in rows}
    values = [matrix[a][b] for a in nodes for b in nodes if a != b]
    asymmetric = [
        (a, b, matrix[a][b], matrix[b][a])
        for i, a in enumerate(nodes)
        for b in nodes[i + 1 :]
        if abs(matrix[a][b] - matrix[b][a]) > 1e-9
    ]
    nonzero_diagonal = [(node, matrix[node][node]) for node in nodes if matrix[node][node] != 0]
    triangle_violations = 0
    for a in nodes:
        for b in nodes:
            direct = matrix[a][b]
            if any(direct > matrix[a][c] + matrix[c][b] + 1e-9 for c in nodes):
                triangle_violations += 1
    nearest_airport = {}
    airports = [node for node in nodes if node.startswith("A")]
    facilities = [node for node in nodes if node.startswith("F")]
    for facility in facilities:
        ranked = sorted((matrix[airport][facility], airport) for airport in airports)
        nearest_airport[facility] = {"airport": ranked[0][1], "distance": ranked[0][0]}
    nearest_counts = Counter(x["airport"] for x in nearest_airport.values())
    aircraft = {
        "T1": {"seats": 12, "speed": 250, "burn": 3.4, "tank": 1000, "reserve": 150},
        "T2": {"seats": 16, "speed": 220, "burn": 2.5, "tank": 1150, "reserve": 150},
        "T3": {"seats": 19, "speed": 190, "burn": 2.9, "tank": 1600, "reserve": 200},
    }
    refuel_nodes = {"F006", "F011", "F018", "F024", "F031", "F038", "F044", "F050"}

    def closed_route_reachability(airport: str, max_range: int) -> dict[str, int]:
        bit = {node: 1 << index for index, node in enumerate(facilities)}
        states: dict[tuple[str, int], int] = {(airport, 0): 0}
        reached_at: dict[str, int] = {}
        for stop_count in range(1, 6):
            next_states: dict[tuple[str, int], int] = {}
            for (current, used), visited in states.items():
                for target in facilities:
                    arrival_used = used + int(matrix[current][target])
                    if arrival_used > max_range:
                        continue
                    new_visited = visited | bit[target]
                    keys = [(target, arrival_used)]
                    if target in refuel_nodes:
                        keys.append((target, 0))
                    for key in keys:
                        next_states[key] = next_states.get(key, 0) | new_visited
            states = next_states
            closed_mask = 0
            for (current, used), visited in states.items():
                if used + matrix[current][airport] <= max_range + 1e-9:
                    closed_mask |= visited
            for facility in facilities:
                if facility not in reached_at and closed_mask & bit[facility]:
                    reached_at[facility] = stop_count
        return reached_at

    aircraft_range = {}
    for aircraft_type, spec in aircraft.items():
        usable_km = int((spec["tank"] - spec["reserve"]) // spec["burn"])
        by_airport = {airport: closed_route_reachability(airport, usable_km) for airport in airports}
        min_stops = {
            facility: min(
                (mapping[facility] for mapping in by_airport.values() if facility in mapping),
                default=None,
            )
            for facility in facilities
        }
        stop_distribution = Counter(value if value is not None else "unreachable" for value in min_stops.values())
        aircraft_range[aircraft_type] = {
            "usable_range_km_floor": usable_km,
            "directed_edge_count_beyond_range": sum(
                matrix[a][b] > usable_km for a in nodes for b in nodes if a != b
            ),
            "min_closed_route_sea_stops_distribution": {str(k): v for k, v in stop_distribution.items()},
            "unreachable_within_5_stops": [facility for facility, value in min_stops.items() if value is None],
            "facilities_requiring_at_least_3_stops": [
                facility for facility, value in min_stops.items() if value is not None and value >= 3
            ],
        }
    return {
        "shape": [len(rows), len(nodes)],
        "airports": airports,
        "facility_count": len(facilities),
        "min_off_diagonal": min(values),
        "max_off_diagonal": max(values),
        "mean_off_diagonal": statistics.mean(values),
        "asymmetric_pair_count": len(asymmetric),
        "asymmetric_examples": asymmetric[:10],
        "nonzero_diagonal": nonzero_diagonal,
        "triangle_violation_pair_count": triangle_violations,
        "nearest_airport_counts": dict(sorted(nearest_counts.items())),
        "nearest_airport": nearest_airport,
        "aircraft_range": aircraft_range,
    }


def node_kind(node: str) -> str:
    if node == "LAND":
        return "LAND"
    if node.startswith("A"):
        return "AIRPORT"
    if node.startswith("F"):
        return "FACILITY"
    return "OTHER"


def summarize_people(path: Path, nearest_airport: dict[str, dict[str, object]] | None = None) -> dict[str, object]:
    rows = read_csv(path)
    ids = [row["person_id"] for row in rows]
    pair_types = Counter(f"{node_kind(row['origin_id'])}->{node_kind(row['destination_id'])}" for row in rows)
    origins = Counter(row["origin_id"] for row in rows)
    destinations = Counter(row["destination_id"] for row in rows)
    exact_pairs = Counter((row["origin_id"], row["destination_id"]) for row in rows)
    summary: dict[str, object] = {
        "row_count": len(rows),
        "headers": list(rows[0]),
        "unique_person_count": len(set(ids)),
        "duplicate_person_ids": [item for item, count in Counter(ids).items() if count > 1],
        "self_trip_count": sum(row["origin_id"] == row["destination_id"] for row in rows),
        "pair_types": dict(sorted(pair_types.items())),
        "top_origins": origins.most_common(12),
        "top_destinations": destinations.most_common(12),
        "top_exact_pairs": [(f"{a}->{b}", n) for (a, b), n in exact_pairs.most_common(15)],
        "unknown_nodes": sorted(
            {
                node
                for row in rows
                for node in (row["origin_id"], row["destination_id"])
                if node_kind(node) == "OTHER"
            }
        ),
        "blank_counts": {key: sum(not row[key].strip() for row in rows) for key in rows[0]},
    }
    if nearest_airport:
        flexible_land_assignment = Counter()
        for row in rows:
            if row["origin_id"] == "LAND":
                flexible_land_assignment[f"outbound_{nearest_airport[row['destination_id']]['airport']}"] += 1
            if row["destination_id"] == "LAND":
                flexible_land_assignment[f"inbound_{nearest_airport[row['origin_id']]['airport']}"] += 1
        summary["flexible_land_nearest_airport_counts"] = dict(sorted(flexible_land_assignment.items()))
    if "task_type" in rows[0]:
        fmt = "%Y-%m-%d %H:%M"
        starts = [datetime.strptime(row["earliest_pickup_time"], fmt) for row in rows]
        ends = [datetime.strptime(row["latest_arrival_time"], fmt) for row in rows]
        windows_hours = [(end - start).total_seconds() / 3600 for start, end in zip(starts, ends)]
        bad_windows = [row["person_id"] for row, start, end in zip(rows, starts, ends) if end < start]
        by_type = Counter(row["task_type"] for row in rows)
        by_day = Counter(start.strftime("%Y-%m-%d") for start in starts)
        summary.update(
            {
                "task_types": dict(sorted(by_type.items())),
                "earliest_pickup_min": min(starts).isoformat(sep=" "),
                "earliest_pickup_max": max(starts).isoformat(sep=" "),
                "latest_arrival_min": min(ends).isoformat(sep=" "),
                "latest_arrival_max": max(ends).isoformat(sep=" "),
                "window_hours": {
                    "min": min(windows_hours),
                    "median": statistics.median(windows_hours),
                    "mean": statistics.mean(windows_hours),
                    "max": max(windows_hours),
                },
                "bad_window_count": len(bad_windows),
                "bad_window_examples": bad_windows[:10],
                "pickup_by_day": dict(sorted(by_day.items())),
                "window_width_buckets": dict(
                    sorted(
                        Counter(
                            "<2h"
                            if value < 2
                            else "2-4h"
                            if value < 4
                            else "4-8h"
                            if value < 8
                            else "8-24h"
                            if value < 24
                            else "24-48h"
                            if value < 48
                            else ">=48h"
                            for value in windows_hours
                        ).items()
                    )
                ),
                "by_task_type": {
                    task_type: {
                        "count": len(group),
                        "window_hours_min": min(item[2] for item in group),
                        "window_hours_median": statistics.median(item[2] for item in group),
                        "window_hours_mean": statistics.mean(item[2] for item in group),
                    }
                    for task_type in sorted(by_type)
                    for group in [[
                        (row, start, (end - start).total_seconds() / 3600)
                        for row, start, end in zip(rows, starts, ends)
                        if row["task_type"] == task_type
                    ]]
                },
            }
        )
    return summary


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    pdfs = sorted(root.rglob("*.pdf"))
    csvs = sorted(root.rglob("*.csv"))
    result: dict[str, object] = {
        "pdfs": [extract_pdf(path) for path in pdfs],
        "csv_headers": {},
        "people": {},
    }
    distances_summary = None
    for path in csvs:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            result["csv_headers"][path.name] = next(reader)
        if path.name == "distances.csv":
            distances_summary = summarize_distances(path)
            result["distances"] = distances_summary
    for path in csvs:
        if path.name.startswith("peopleQ"):
            result["people"][path.name] = summarize_people(
                path, distances_summary["nearest_airport"] if distances_summary else None
            )
    q1_path = next(path for path in csvs if path.name == "peopleQ1.csv")
    q2_path = next(path for path in csvs if path.name == "peopleQ2.csv")
    q3_path = next(path for path in csvs if path.name == "peopleQ3.csv")
    q1 = {row["person_id"]: row for row in read_csv(q1_path)}
    q2 = {row["person_id"]: row for row in read_csv(q2_path)}
    q3 = {row["person_id"]: row for row in read_csv(q3_path)}
    result["cross_file_consistency"] = {
        "q1_ids_subset_q2": set(q1) <= set(q2),
        "q1_od_matches_q2": all(
            q1[person]["origin_id"] == q2[person]["origin_id"]
            and q1[person]["destination_id"] == q2[person]["destination_id"]
            for person in q1
        ),
        "q2_ids_equal_q3": set(q2) == set(q3),
        "q2_od_matches_q3": all(
            q2[person]["origin_id"] == q3[person]["origin_id"]
            and q2[person]["destination_id"] == q3[person]["destination_id"]
            for person in q2
        ),
        "unique_od_q1": len({(row["origin_id"], row["destination_id"]) for row in q1.values()}),
        "unique_od_q2": len({(row["origin_id"], row["destination_id"]) for row in q2.values()}),
    }
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))


if __name__ == "__main__":
    main()
