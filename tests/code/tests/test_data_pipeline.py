from __future__ import annotations

import json

from src.config import ROOT, load_config
from src.data_pipeline import analyze_triangle_inequality, load_distance_matrix
from src.io_utils import read_csv


def test_distance_matrix_reading_and_shape(raw_dir):
    nodes, matrix = load_distance_matrix(raw_dir / "distances.csv")
    assert len(nodes) == 55
    assert set(matrix) == set(nodes)
    assert matrix["A01"]["F006"] == 235
    assert matrix["F006"]["A01"] == 235


def test_aircraft_parameters_and_fleet(config):
    assert config.aircraft_types["T1"].seats == 12
    assert config.aircraft_types["T2"].speed_kmh == 220
    assert config.aircraft_types["T3"].reserve_kg == 200
    assert len(config.fleet_ids) == 24
    assert "A01-T2-H03" in config.fleet_ids


def test_land_candidates_are_not_collapsed():
    row = next(row for row in read_csv(ROOT / "data" / "processed" / "demands_q1.csv") if row["origin"] == "LAND")
    assert json.loads(row["candidate_origin_airports"]) == ["A01", "A02", "A03"]
    assert row["fixed_origin_airport"] == ""
    assert row["nearest_airport"] in {"A01", "A02", "A03"}


def test_leg_feature_regression_a01_f006_t2():
    row = next(
        row
        for row in read_csv(ROOT / "data" / "processed" / "features" / "leg_features.csv")
        if row["from_node"] == "A01" and row["to_node"] == "F006" and row["aircraft_type"] == "T2"
    )
    assert int(row["flight_minutes"]) == 65
    assert float(row["fuel_consumption_kg"]) == 587.5


def test_triangle_violation_counts_are_separate(raw_dir):
    nodes, matrix = load_distance_matrix(raw_dir / "distances.csv")
    result = analyze_triangle_inequality(nodes, matrix)
    assert result["unordered_pair_count"] == 147
    assert result["directed_pair_count"] == 294
    assert result["violating_triple_count"] == 442


def test_canonical_counts_and_od_traceability():
    q1 = read_csv(ROOT / "data" / "processed" / "demands_q1.csv")
    q2 = read_csv(ROOT / "data" / "processed" / "demands_q2.csv")
    q3 = read_csv(ROOT / "data" / "processed" / "demands_q3.csv")
    od_q1 = read_csv(ROOT / "data" / "processed" / "od_q1.csv")
    od_q2 = read_csv(ROOT / "data" / "processed" / "od_q2.csv")
    assert (len(q1), len(q2), len(q3)) == (1600, 4000, 4000)
    assert (len(od_q1), len(od_q2)) == (104, 264)
    assert sum(int(row["demand_count"]) for row in od_q1) == len(q1)
    assert sum(len(row["person_ids"].split("|")) for row in od_q2) == len(q2)
