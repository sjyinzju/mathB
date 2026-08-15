from __future__ import annotations

import json
from pathlib import Path

from src.solver import load_problem_data
from src.solver.q3 import (
    load_q3_people,
    load_q3_schedule,
    load_q3_variants,
    schedule_metrics,
    stage1_key,
    stage2_key,
)
from src.solver.q3_pro import (
    ElitePool,
    build_route_library,
    preprocess_neighborhoods,
    solution_distance,
    solution_signature,
)


ROOT = Path(__file__).resolve().parents[1]


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


def test_canonical_objective_api_includes_all_lexicographic_terms() -> None:
    _data, people, _variants, stage1, stage2 = _loaded()
    metrics = schedule_metrics(stage1, people)
    assert len(stage1_key(stage1, people)) == 5
    assert len(stage2_key(stage2, people)) == 6
    assert stage1_key(stage1, people)[0] == metrics["total_aircraft_time_minutes"]
    assert stage1_key(stage1, people)[-1] == -metrics["seat_utilization"]
    assert stage2_key(stage2, people)[0] == -158


def test_preprocessing_dominance_is_semantics_safe_and_cache_is_hot() -> None:
    data, people, variants, _stage1, _stage2 = _loaded()
    filtered, cache, report = preprocess_neighborhoods(people, variants, data)
    assert report["candidate_count_after"] <= report["candidate_count_before"]
    assert "no cross-order dominance" in report["dominance_rule"]
    assert all(filtered[od] for od in variants)
    assert cache.person_route_day
    assert cache.route_day_aircraft
    assert report["cache"]["hits"] > 0


def test_elite_pool_rejects_exact_duplicate_and_tracks_diversity() -> None:
    _data, people, _variants, stage1, stage2 = _loaded()
    mandatory = {pid: person for pid, person in people.items() if person.mandatory}
    pool = ElitePool(mandatory, stage=1, maximum_size=4)
    assert pool.add(stage1, source="best", seed=1)
    assert not pool.add(stage1, source="duplicate", seed=2)
    assert solution_signature(stage1) == solution_signature(stage1)
    assert solution_distance(stage1, stage1) == 0.0
    # Stage 2 has the same structure and days, so it is a near duplicate even
    # though its passenger assignment includes optional people.
    assert 0.0 <= solution_distance(stage1, stage2) <= 1.0
    assert pool.summary()["size"] == 1


def test_route_library_is_persistent_and_does_not_touch_q2_cache(tmp_path) -> None:
    _data, _people, variants, stage1, _stage2 = _loaded()
    cache = ROOT / "outputs/q2/pair_n3_h10.pkl"
    before = cache.stat().st_mtime_ns
    target = tmp_path / "route_library" / "routes.json"
    summary = build_route_library(
        variants, stage1, source="test", path=target
    )
    records = json.loads(target.read_text(encoding="utf-8"))
    assert summary["route_count"] == len(records)
    assert summary["used_by_final"] > 0
    assert cache.stat().st_mtime_ns == before
