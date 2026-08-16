from __future__ import annotations

import csv
import importlib.util
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("inspect_b_sources", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    module = load_module(root / "scripts" / "inspect_b_sources.py")
    csvs = {path.name: path for path in root.rglob("*.csv")}
    distances = module.summarize_distances(csvs["distances.csv"])
    people = {
        name: module.summarize_people(path, distances["nearest_airport"])
        for name, path in csvs.items()
        if name.startswith("peopleQ")
    }

    with csvs["peopleQ3.csv"].open("r", encoding="utf-8-sig", newline="") as stream:
        q3 = list(csv.DictReader(stream))
    fmt = "%Y-%m-%d %H:%M"
    tight_by_type_and_trip = Counter()
    same_day_by_type = Counter()
    pickup_hour_by_type = Counter()
    deadline_hour_by_type = Counter()
    for row in q3:
        start = datetime.strptime(row["earliest_pickup_time"], fmt)
        end = datetime.strptime(row["latest_arrival_time"], fmt)
        hours = (end - start).total_seconds() / 3600
        trip = f"{module.node_kind(row['origin_id'])}->{module.node_kind(row['destination_id'])}"
        if hours < 8:
            tight_by_type_and_trip[(row["task_type"], trip)] += 1
        if start.date() == end.date():
            same_day_by_type[row["task_type"]] += 1
        pickup_hour_by_type[(row["task_type"], start.hour)] += 1
        deadline_hour_by_type[(row["task_type"], end.hour)] += 1

    compact = {
        "distance": {
            key: distances[key]
            for key in (
                "shape",
                "facility_count",
                "min_off_diagonal",
                "max_off_diagonal",
                "mean_off_diagonal",
                "asymmetric_pair_count",
                "nonzero_diagonal",
                "triangle_violation_pair_count",
                "nearest_airport_counts",
                "aircraft_range",
            )
        },
        "people": {
            name: {
                key: summary[key]
                for key in summary
                if key
                in {
                    "row_count",
                    "unique_person_count",
                    "duplicate_person_ids",
                    "self_trip_count",
                    "pair_types",
                    "blank_counts",
                    "flexible_land_nearest_airport_counts",
                    "task_types",
                    "window_hours",
                    "window_width_buckets",
                    "by_task_type",
                    "pickup_by_day",
                }
            }
            for name, summary in people.items()
        },
        "q3_tight_under_8h_by_type_trip": {
            f"{task}|{trip}": count for (task, trip), count in sorted(tight_by_type_and_trip.items())
        },
        "q3_same_day_by_type": dict(sorted(same_day_by_type.items())),
        "q3_pickup_hour_by_type": {
            f"{task}|{hour:02d}": count for (task, hour), count in sorted(pickup_hour_by_type.items())
        },
        "q3_deadline_hour_by_type": {
            f"{task}|{hour:02d}": count for (task, hour), count in sorted(deadline_hour_by_type.items())
        },
    }
    sys.stdout.buffer.write(json.dumps(compact, ensure_ascii=False, indent=2).encode("utf-8"))


if __name__ == "__main__":
    main()
