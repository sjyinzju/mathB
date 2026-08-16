from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

from src.solver import load_problem_data
from src.solver.q3 import load_q3_people, load_q3_schedule, load_q3_variants
from src.solver.q3_closure_p2 import replacement_time_admissible
from src.solver.q3_pro_v2 import (
    aggregate_convergence,
    atomic_write_json,
    build_flight_column_library,
    critical_leg_graph,
    find_ejection_chains,
    kempe_exchange_cycles,
    optional_rescue_dossier_v2,
    parameter_grid,
    pricing_guided_variant_pool,
)


ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _loaded():
    data = load_problem_data()
    people = load_q3_people(ROOT / "data/raw/peopleQ3.csv", data.config)
    variants = load_q3_variants(
        ROOT / "outputs/q2/pair_n3_h10.pkl", people.values(), data.config
    )
    stage1 = load_q3_schedule(
        ROOT / "outputs/q3/best/q3-base-routes.csv",
        ROOT / "outputs/q3/best/q3-base-assignments.csv",
        people,
        variants,
        data.config,
    )
    stage2 = load_q3_schedule(
        ROOT / "outputs/q3/best/q3-routes.csv",
        ROOT / "outputs/q3/best/q3-assignments.csv",
        people,
        variants,
        data.config,
    )
    return data, people, variants, stage1, stage2


def test_stage2_equal_time_replacement_is_admissible() -> None:
    assert not replacement_time_admissible(1, 100, 100)
    assert replacement_time_admissible(1, 99, 100)
    assert replacement_time_admissible(2, 100, 100)
    assert not replacement_time_admissible(2, 101, 100)
    with pytest.raises(ValueError):
        replacement_time_admissible(3, 1, 2)


def test_parameter_portfolio_is_heterogeneous_and_reproducible() -> None:
    left = parameter_grid(20260816, 20)
    right = parameter_grid(20260816, 20)
    assert left == right
    assert len({row.seed for row in left}) == 20
    assert len({row.operator_profile for row in left}) == 4
    assert len({row.heavy_group_max for row in left}) >= 3
    assert max(row.cross_day_trials for row in left) >= 12


def test_critical_leg_graph_uses_leg_occupancy_not_flight_headcount() -> None:
    _data, people, _variants, _stage1, stage2 = _loaded()
    graph = critical_leg_graph(stage2, people)
    assert graph["critical_leg_count"] > 0
    assert all(row["load"] == row["capacity"] for row in graph["critical_legs"])
    assert all(row["occupants"] for row in graph["critical_legs"])


def test_optional_dossier_covers_ejection_and_land_base_reassignment() -> None:
    data, people, variants, _stage1, stage2 = _loaded()
    dossier = optional_rescue_dossier_v2(stage2, people, variants, data)
    assert {row["person_id"] for row in dossier["records"]} == {
        "P1102",
        "P2239",
        "P3290",
    }
    land = next(row for row in dossier["records"] if row["person_id"] == "P2239")
    assert land["base_reassignment_relevant"]
    assert len(land["compatible_bases"]) >= 1
    chains = find_ejection_chains(dossier, maximum_depth=4)
    assert all(1 <= row["depth"] <= 4 for row in chains)


def test_kempe_cycle_synthetic() -> None:
    current = {"A": "F1", "B": "F2", "C": "F3"}
    compatible = {"A": ["F2"], "B": ["F3"], "C": ["F1"]}
    cycles = kempe_exchange_cycles(current, compatible, maximum_length=4)
    assert ["A", "B", "C"] in cycles


def test_pricing_columns_are_imported_to_primal_pool() -> None:
    _data, _people, variants, _stage1, _stage2 = _loaded()
    od = next(iter(variants))
    selected = variants[od][0]
    pricing = {
        "priced_full_pool_master": {
            "details": {
                "top_selected_routes": [{"route_key": repr(selected.key)}],
                "top_od_duals": [{"od": list(od), "dual_minutes": 99.0}],
            }
        }
    }
    pool, report = pricing_guided_variant_pool(variants, pricing, per_od=3)
    assert selected in pool[od]
    assert report["selected_routes_imported"] >= 1
    assert all(1 <= len(values) <= 3 for values in pool.values())


def test_flight_column_library_deduplicates_elites(tmp_path: Path) -> None:
    _data, people, _variants, stage1, _stage2 = _loaded()
    target = tmp_path / "column_library" / "columns.json"
    report = build_flight_column_library(
        [("elite-a", stage1), ("elite-b", stage1)], people, path=target
    )
    records = json.loads(target.read_text(encoding="utf-8"))
    assert report["column_count"] == len(stage1)
    assert all(row["elite_frequency"] == 2 for row in records)


def test_checkpoint_and_worker_aggregation_are_resumable(tmp_path: Path) -> None:
    target = tmp_path / "checkpoints" / "screen.json"
    atomic_write_json(target, {"completed": 7, "phase": "screen"})
    assert json.loads(target.read_text(encoding="utf-8"))["completed"] == 7
    rows = aggregate_convergence(
        [
            {
                "runtime_seconds": 2.0,
                "trace": {
                    "convergence": [
                        {
                            "elapsed_seconds": 1.0,
                            "best_aircraft_time": 29150,
                            "elite_size": 4,
                        }
                    ]
                },
            }
        ],
        baseline_ub=29155,
        stage2_optional=157,
        global_lb=14125,
        restricted_lp=15197.677,
        route_count=3116,
        column_count=165,
    )
    assert rows[-1]["stage1_ub"] == 29150
    assert rows[-1]["elite_pool_size"] == 4
