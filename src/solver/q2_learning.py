from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


LABELS = ("POSITIVE", "TRUE_NEGATIVE", "CENSORED", "INVALID")
OUTCOME_FIELDS = frozenset(
    {
        "label_class",
        "selected_for_exact",
        "exact_variant_generated",
        "entered_local_master",
        "milp_candidate",
        "milp_selected",
        "repair_feasible",
        "repair_accepted",
        "primary_gain",
        "secondary_gain",
        "new_global_best",
        "evaluation_cost_ms",
        "evaluation_state",
        "label_censored",
    }
)


def classify_q2_candidate_event(row: dict[str, object]) -> str:
    if row.get("evaluation_state") != "exact_evaluated":
        return "CENSORED"
    if not row.get("exact_variant_generated"):
        return "INVALID"
    if (
        row.get("milp_selected")
        and row.get("repair_accepted")
        and (float(row.get("primary_gain", 0)) > 0 or float(row.get("secondary_gain", 0)) > 0)
    ):
        return "POSITIVE"
    return "TRUE_NEGATIVE"


def _scalar(value: object) -> object:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return value


def flatten_q2_candidate_event(row: dict[str, object]) -> dict[str, object]:
    flat = {key: _scalar(value) for key, value in row.items() if key not in {"features", "search_context"}}
    for prefix, nested in (("feature", row.get("features")), ("search", row.get("search_context"))):
        if isinstance(nested, dict):
            for key, value in nested.items():
                flat[f"{prefix}_{key}"] = _scalar(value)
    flat["label_class"] = classify_q2_candidate_event(row)
    return flat


def grouped_q2_splits(run_rows: Iterable[dict[str, object]]) -> dict[str, str]:
    rows = tuple(run_rows)
    groups = sorted({str(row.get("lineage_id") or row["run_id"]) for row in rows})
    run_ids = sorted({str(row["run_id"]) for row in rows})
    if not run_ids:
        return {}
    ordered = sorted(
        groups,
        key=lambda value: (hashlib.sha256(value.encode("utf-8")).hexdigest(), value),
    )
    if len(ordered) == 1:
        by_group = {ordered[0]: "train"}
    elif len(ordered) == 2:
        by_group = {ordered[0]: "train", ordered[1]: "test"}
    else:
        by_group = {
            group: ("validation", "test", "train")[index % 3]
            for index, group in enumerate(ordered)
        }
    return {
        str(row["run_id"]): by_group[str(row.get("lineage_id") or row["run_id"])]
        for row in rows
    }


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_q2_learning_dataset(
    run_dirs: Iterable[Path],
    output_dir: Path,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_rows: list[dict[str, object]] = []
    repair_rows: list[dict[str, object]] = []
    run_rows: list[dict[str, object]] = []
    seen_ids: Counter[str] = Counter()
    for run_dir in sorted(run_dirs):
        config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
        run_id = str(config["run_id"])
        run_rows.append(
            {
                "run_id": run_id,
                "seed": config["config"]["seed"],
                "purpose": config["config"].get("run_purpose", "optimization"),
                "candidate_policy": config["config"]["candidate_policy"],
                "git_commit": config["git_commit"],
                "source_routes_sha256": config["source"]["routes_sha256"],
                "source_assignments_sha256": config["source"]["assignments_sha256"],
                "lineage_id": config["config"].get("lineage_id") or run_id,
                "parent_run_id": config["config"].get("parent_run_id"),
                "parent_solution_hash": config["config"].get("parent_solution_hash"),
                "warm_start_hash": config["config"].get("warm_start_hash"),
                "elite_origin": config["config"].get("elite_origin"),
                "restart_type": config["config"].get("restart_type", "direct"),
            }
        )
        with (run_dir / "candidate-log.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                flat = flatten_q2_candidate_event(row)
                candidate_rows.append(flat)
                seen_ids[str(flat.get("candidate_id", ""))] += 1
        with (run_dir / "search-log.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                repair_rows.append({"run_id": run_id, **{key: _scalar(value) for key, value in row.items()}})

    splits = grouped_q2_splits(run_rows)
    split_rows = [
        {"run_id": row["run_id"], "lineage_id": row["lineage_id"], "seed": row["seed"], "split": splits[str(row["run_id"])]}
        for row in run_rows
    ]
    labels = Counter(str(row["label_class"]) for row in candidate_rows)
    by_run: dict[str, Counter[str]] = defaultdict(Counter)
    for row in candidate_rows:
        by_run[str(row["run_id"])][str(row["label_class"])] += 1
    feature_fields = sorted(
        key for key in {key for row in candidate_rows for key in row} if key.startswith("feature_") or key.startswith("search_")
    )
    missing = {
        field: sum(row.get(field) in {None, ""} for row in candidate_rows)
        for field in feature_fields
    }
    split_positive = Counter()
    for run_id, counts in by_run.items():
        split_positive[splits[run_id]] += counts["POSITIVE"]
    positive_rows = [row for row in candidate_rows if row["label_class"] == "POSITIVE"]
    novel_positive_rows = [
        row for row in positive_rows
        if float(row.get("sequence_novelty") or 0) > 0
        or not bool(row.get("is_incumbent_sequence"))
    ]
    split_novel_positive = Counter()
    source_exact = Counter()
    source_positive = Counter()
    rank_bin_exact = Counter()
    rank_bin_positive = Counter()
    for row in candidate_rows:
        source = str(row.get("candidate_source") or "OTHER")
        rank_bin = str(row.get("geometry_rank_bin") or "unavailable")
        if row["label_class"] != "CENSORED":
            source_exact[source] += 1
            rank_bin_exact[rank_bin] += 1
        if row["label_class"] == "POSITIVE":
            source_positive[source] += 1
            rank_bin_positive[rank_bin] += 1
    for row in novel_positive_rows:
        split_novel_positive[splits[str(row["run_id"])]] += 1
    positive_lineages = {str(row.get("search_lineage_id") or row["run_id"]) for row in positive_rows}
    novel_positive_lineages = {str(row.get("search_lineage_id") or row["run_id"]) for row in novel_positive_rows}
    diagnostics = {
        "total_candidate_rows": len(candidate_rows),
        "exact_evaluated_rows": labels["POSITIVE"] + labels["TRUE_NEGATIVE"] + labels["INVALID"],
        "censored_rows": labels["CENSORED"],
        "true_negative_rows": labels["TRUE_NEGATIVE"],
        "positive_rows": labels["POSITIVE"],
        "invalid_rows": labels["INVALID"],
        "accepted_repairs": sum(int(bool(row.get("accepted"))) for row in repair_rows),
        "new_best_repairs": sum(int(bool(row.get("new_best"))) for row in repair_rows),
        "run_count": len(run_rows),
        "label_distribution_by_run": {key: dict(value) for key, value in sorted(by_run.items())},
        "positive_distribution_by_split": dict(split_positive),
        "novel_positive_rows": len(novel_positive_rows),
        "non_incumbent_positive_rows": sum(int(not bool(row.get("is_incumbent_sequence"))) for row in positive_rows),
        "positive_lineage_count": len(positive_lineages),
        "novel_positive_lineage_count": len(novel_positive_lineages),
        "novel_positive_distribution_by_split": dict(split_novel_positive),
        "exact_distribution_by_candidate_source": dict(source_exact),
        "positive_distribution_by_candidate_source": dict(source_positive),
        "geometry_rank_bin_exact_coverage": dict(rank_bin_exact),
        "geometry_rank_bin_positive_coverage": dict(rank_bin_positive),
        "duplicate_candidate_ids": sum(count - 1 for key, count in seen_ids.items() if key and count > 1),
        "missing_feature_counts": missing,
        "feature_count": len(feature_fields),
        "run_group_leakage": False,
        "lineage_group_leakage": False,
        "outcome_fields_excluded_from_feature_schema": True,
    }
    _write_csv(output_dir / "candidate_events.csv", candidate_rows)
    _write_csv(output_dir / "repair_events.csv", repair_rows)
    _write_csv(output_dir / "run_manifest.csv", run_rows)
    _write_csv(output_dir / "split_manifest.csv", split_rows)
    (output_dir / "feature_schema.json").write_text(
        json.dumps(
            {
                "version": 2,
                "features": feature_fields,
                "forbidden_outcome_fields": sorted(OUTCOME_FIELDS),
                "note": "Feature fields are generated before exact/MILP outcomes.",
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    (output_dir / "label_schema.json").write_text(
        json.dumps(
            {
                "version": 2,
                "labels": {
                    "POSITIVE": "exact-evaluated, MILP-selected, accepted useful repair",
                    "TRUE_NEGATIVE": "exact-evaluated but not useful",
                    "CENSORED": "not selected for exact evaluation",
                    "INVALID": "selected but no legal exact variant",
                },
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    (output_dir / "dataset_diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return diagnostics
