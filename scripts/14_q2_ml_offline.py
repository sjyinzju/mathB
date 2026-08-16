from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, LGBMRanker, early_stopping, log_evaluation
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


NUMERIC_FEATURES = (
    "feature_bidirectional_flow",
    "feature_capacity_fit",
    "feature_current_duration_minutes",
    "feature_current_land_fraction",
    "feature_current_mean_residual_slack",
    "feature_current_mean_stop_count",
    "feature_current_mean_utilization",
    "feature_current_min_residual_slack",
    "feature_current_route_count",
    "feature_current_route_passengers",
    "feature_detour_ratio",
    "feature_directed_shuttle_flow",
    "feature_ejection_coverage",
    "feature_fixed_airport_affinity",
    "feature_flow_complementarity",
    "feature_inbound_flow",
    "feature_incumbent_sequence",
    "feature_land_flexible_flow",
    "feature_local_supported_demand",
    "feature_max_airport_distance_km",
    "feature_max_pairwise_distance_km",
    "feature_min_airport_distance_km",
    "feature_min_pairwise_distance_km",
    "feature_outbound_flow",
    "feature_refuel_facilities",
    "feature_reverse_shuttle_flow",
    "feature_route_distance_km",
    "feature_score",
    "feature_seat_reuse_proxy",
    "feature_service_node_count",
    "feature_technical_stop_complexity_proxy",
    "search_best_objective",
    "search_current_objective",
    "search_cross_exchange_flag",
    "search_destroy_size",
    "search_iteration",
    "search_relink_flag",
    "search_sa_temperature",
    "search_stagnation_length",
    "search_warm_start_flights",
    "search_warm_start_objective",
    "rank_score_geometry",
    "rank_score_context",
    "is_incumbent_sequence",
    "seen_in_current_solution",
    "seen_in_elite_pool",
    "seen_in_previous_run",
    "sequence_novelty",
    "structural_novelty",
)

CATEGORICAL_FEATURES = (
    "candidate_source",
    "geometry_rank_bin",
    "context_rank_bin",
    "destroy_operator",
    "portfolio_source",
    "search_destroy_operator",
    "search_restart_type",
    "search_run_purpose",
    "search_targeted_trigger",
    "feature_current_aircraft_types",
    "feature_current_base_airports",
)

IDENTITY_FIELDS = frozenset(
    {
        "candidate_id",
        "run_id",
        "seed",
        "feature_sequence",
        "candidate_sequence",
        "search_lineage_id",
        "search_parent_run_id",
        "search_parent_solution_hash",
        "search_warm_start_hash",
        "search_elite_origin",
    }
)

AUDIT_FIELDS = (
    "candidate_id",
    "candidate_sequence",
    "candidate_source",
    "geometry_rank_bin",
    "run_id",
    "iteration",
    "destroy_operator",
    "destroy_size",
    "source_routes",
    "label_class",
    "is_incumbent_sequence",
    "sequence_novelty",
    "structural_novelty",
)


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_data(path: Path) -> pd.DataFrame:
    header = pd.read_csv(path, nrows=0).columns.tolist()
    wanted = list(dict.fromkeys((*AUDIT_FIELDS, *NUMERIC_FEATURES, *CATEGORICAL_FEATURES)))
    usecols = [column for column in wanted if column in header]
    frame = pd.read_csv(path, usecols=usecols, low_memory=False)
    for column in NUMERIC_FEATURES:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in CATEGORICAL_FEATURES:
        if column in frame:
            frame[column] = frame[column].fillna("__MISSING__").astype(str)
    return frame


def _novel(frame: pd.DataFrame) -> pd.Series:
    incumbent = frame.get("is_incumbent_sequence", False)
    if not isinstance(incumbent, pd.Series):
        incumbent = pd.Series(False, index=frame.index)
    incumbent = incumbent.astype(str).str.lower().isin({"true", "1"})
    novelty = pd.to_numeric(frame.get("sequence_novelty", 0), errors="coerce").fillna(0)
    return (~incumbent) | novelty.gt(0)


def _group_key(frame: pd.DataFrame) -> pd.Series:
    fields = ["run_id", "iteration", "destroy_operator", "destroy_size", "source_routes"]
    return frame[fields].fillna("").astype(str).agg("|".join, axis=1)


def _preprocessor(numeric: list[str], categorical: list[str]) -> ColumnTransformer:
    return ColumnTransformer(
        [
            (
                "numeric",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="median", add_indicator=True)),
                        ("scale", StandardScaler(with_mean=False)),
                    ]
                ),
                numeric,
            ),
            (
                "categorical",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="most_frequent")),
                        (
                            "onehot",
                            OneHotEncoder(
                                handle_unknown="ignore",
                                min_frequency=20,
                                sparse_output=True,
                            ),
                        ),
                    ]
                ),
                categorical,
            ),
        ],
        sparse_threshold=0.2,
    )


def _score_metrics(
    frame: pd.DataFrame,
    scores: np.ndarray,
    *,
    ks: tuple[int, ...],
) -> dict[str, object]:
    labels = frame["target"].to_numpy(dtype=int)
    novel_positive = (frame["novel"].to_numpy(dtype=bool) & (labels == 1))
    groups = frame["group_key"].to_numpy()
    result: dict[str, object] = {
        "rows": int(len(frame)),
        "positives": int(labels.sum()),
        "novel_positives": int(novel_positive.sum()),
        "prevalence": float(labels.mean()),
        "pr_auc": float(average_precision_score(labels, scores)),
        "roc_auc": float(roc_auc_score(labels, scores)),
        "brier": float(brier_score_loss(labels, np.clip(scores, 0.0, 1.0))),
        "top_k": {},
    }
    ordered = pd.DataFrame(
        {
            "group": groups,
            "label": labels,
            "novel_positive": novel_positive,
            "score": scores,
            "tie": np.arange(len(frame)),
        }
    ).sort_values(["group", "score", "tie"], ascending=[True, False, True])
    positive_groups = set(ordered.loc[ordered["label"].eq(1), "group"])
    for k in ks:
        top = ordered.groupby("group", sort=False).head(k)
        tp = int(top["label"].sum())
        novel_tp = int(top["novel_positive"].sum())
        hit_groups = set(top.loc[top["label"].eq(1), "group"])
        result["top_k"][str(k)] = {
            "selected": int(len(top)),
            "precision": float(tp / max(1, len(top))),
            "recall": float(tp / max(1, labels.sum())),
            "novel_positive_precision": float(novel_tp / max(1, len(top))),
            "novel_positive_recall": float(novel_tp / max(1, novel_positive.sum())),
            "lift": float((tp / max(1, len(top))) / max(labels.mean(), 1.0e-12)),
            "positive_group_hit_rate": float(
                len(hit_groups) / max(1, len(positive_groups))
            ),
        }
    return result


def _percentile_within_group(frame: pd.DataFrame, values: np.ndarray) -> np.ndarray:
    work = pd.DataFrame({"group": frame["group_key"].to_numpy(), "value": values})
    return work.groupby("group")["value"].rank(method="average", pct=True).to_numpy()


def _importance(
    preprocessor: ColumnTransformer,
    values: np.ndarray,
    *,
    kind: str,
) -> pd.DataFrame:
    names = preprocessor.get_feature_names_out()
    return (
        pd.DataFrame({"feature": names, "importance": values, "kind": kind})
        .assign(abs_importance=lambda frame: frame["importance"].abs())
        .sort_values("abs_importance", ascending=False)
        .drop(columns="abs_importance")
    )


def _rank_sort(
    frame: pd.DataFrame, matrix,
) -> tuple[pd.DataFrame, object, list[int]]:
    order = np.argsort(frame["group_key"].to_numpy(), kind="stable")
    sorted_frame = frame.iloc[order].reset_index(drop=True)
    sorted_matrix = matrix[order]
    groups = sorted_frame.groupby("group_key", sort=False).size().tolist()
    return sorted_frame, sorted_matrix, groups


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit and train Q2 final offline rankers")
    parser.add_argument("--candidate-events", type=Path, required=True)
    parser.add_argument(
        "--dataset-dir", type=Path, default=ROOT / "outputs/q2/ml-data-round3"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "outputs/q2/final-ml/offline"
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    schema = _json(args.dataset_dir / "feature_schema.json")
    labels_schema = _json(args.dataset_dir / "label_schema.json")
    split_manifest = pd.read_csv(args.dataset_dir / "split_manifest.csv")
    frame = _load_data(args.candidate_events)
    split_map = dict(zip(split_manifest["run_id"], split_manifest["split"], strict=True))
    frame["split"] = frame["run_id"].map(split_map)
    frame["novel"] = _novel(frame)
    frame["group_key"] = _group_key(frame)

    label_counts = frame["label_class"].value_counts().to_dict()
    exact = frame[frame["label_class"].isin(["POSITIVE", "TRUE_NEGATIVE"])].copy()
    exact["target"] = exact["label_class"].eq("POSITIVE").astype(int)
    feature_columns = [
        column
        for column in (*NUMERIC_FEATURES, *CATEGORICAL_FEATURES)
        if column in exact.columns and column not in IDENTITY_FIELDS
    ]
    numeric = [column for column in NUMERIC_FEATURES if column in feature_columns]
    categorical = [column for column in CATEGORICAL_FEATURES if column in feature_columns]
    forbidden = set(schema["forbidden_outcome_fields"])
    leakage = sorted(forbidden & set(feature_columns))
    lineage_split_counts = split_manifest.groupby("lineage_group_id")["split"].nunique()

    source = (
        frame.groupby("candidate_source", dropna=False)
        .agg(
            rows=("candidate_id", "size"),
            exact_evaluated=("label_class", lambda values: int(values.isin(["POSITIVE", "TRUE_NEGATIVE", "INVALID"]).sum())),
            positives=("label_class", lambda values: int(values.eq("POSITIVE").sum())),
            novel_positives=("novel", lambda values: 0),
        )
        .reset_index()
    )
    novel_by_source = (
        frame.loc[frame["label_class"].eq("POSITIVE") & frame["novel"]]
        .groupby("candidate_source")
        .size()
    )
    source["novel_positives"] = source["candidate_source"].map(novel_by_source).fillna(0).astype(int)
    source["positive_rate_exact"] = source["positives"] / source["exact_evaluated"].clip(lower=1)
    source["novel_positive_rate_exact"] = source["novel_positives"] / source["exact_evaluated"].clip(lower=1)
    source.to_csv(args.output_dir / "candidate_source_audit.csv", index=False)

    audit = {
        "candidate_rows": int(len(frame)),
        "label_counts": {key: int(value) for key, value in label_counts.items()},
        "exact_supervised_rows": int(len(exact)),
        "duplicate_candidate_ids": int(frame["candidate_id"].duplicated().sum()),
        "lineage_groups": int(split_manifest["lineage_group_id"].nunique()),
        "lineage_split_leakage": bool(lineage_split_counts.gt(1).any()),
        "unmapped_runs": sorted(frame.loc[frame["split"].isna(), "run_id"].unique()),
        "feature_count": len(feature_columns),
        "numeric_features": numeric,
        "categorical_features": categorical,
        "identity_fields_excluded": sorted(IDENTITY_FIELDS),
        "outcome_feature_leakage": leakage,
        "schema_version": schema["version"],
        "label_schema_version": labels_schema["version"],
        "censored_used_as_negative": False,
        "invalid_used_in_main_model": False,
    }
    (args.output_dir / "dataset_audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if audit["duplicate_candidate_ids"] or audit["lineage_split_leakage"] or leakage or audit["unmapped_runs"]:
        raise RuntimeError(f"Dataset audit gate failed: {audit}")

    train = exact[exact["split"].eq("train")].copy()
    validation = exact[exact["split"].eq("validation")].copy()
    test = exact[exact["split"].eq("test")].copy()
    ks = (10, 25, 50)
    rng = np.random.default_rng(20260816)
    scores: dict[str, dict[str, np.ndarray]] = {
        "random": {
            "validation": rng.random(len(validation)),
            "test": rng.random(len(test)),
        },
        "geometry": {
            split: 2.0 * part["is_incumbent_sequence"].astype(str).str.lower().isin({"true", "1"}).to_numpy(dtype=float)
            + pd.to_numeric(part["rank_score_geometry"], errors="coerce").fillna(0).to_numpy()
            for split, part in (("validation", validation), ("test", test))
        },
    }

    lr = Pipeline(
        [
            ("preprocessor", _preprocessor(numeric, categorical)),
            (
                "model",
                LogisticRegression(
                    C=0.3,
                    class_weight="balanced",
                    max_iter=500,
                    solver="liblinear",
                    random_state=20260816,
                ),
            ),
        ]
    )
    lr.fit(train[feature_columns], train["target"])
    scores["logistic_regression"] = {
        "validation": lr.predict_proba(validation[feature_columns])[:, 1],
        "test": lr.predict_proba(test[feature_columns])[:, 1],
    }
    joblib.dump(
        {"kind": "classifier", "feature_columns": feature_columns, "model": lr},
        args.output_dir / "q2_lr_ranker.joblib",
    )
    lr_importance = _importance(
        lr.named_steps["preprocessor"],
        lr.named_steps["model"].coef_[0],
        kind="coefficient",
    )
    lr_importance.to_csv(args.output_dir / "lr_feature_importance.csv", index=False)

    preprocessor = _preprocessor(numeric, categorical)
    train_matrix = preprocessor.fit_transform(train[feature_columns])
    validation_matrix = preprocessor.transform(validation[feature_columns])
    test_matrix = preprocessor.transform(test[feature_columns])
    lgbm_classifier = LGBMClassifier(
        objective="binary",
        n_estimators=400,
        learning_rate=0.03,
        num_leaves=15,
        max_depth=5,
        min_child_samples=100,
        reg_lambda=2.0,
        colsample_bytree=0.8,
        class_weight="balanced",
        random_state=20260816,
        n_jobs=-1,
        verbosity=-1,
    )
    lgbm_classifier.fit(
        train_matrix,
        train["target"],
        eval_set=[(validation_matrix, validation["target"])],
        eval_metric="average_precision",
        callbacks=[early_stopping(30, verbose=False), log_evaluation(0)],
    )
    scores["lightgbm_classifier"] = {
        "validation": lgbm_classifier.predict_proba(validation_matrix)[:, 1],
        "test": lgbm_classifier.predict_proba(test_matrix)[:, 1],
    }
    joblib.dump(
        {
            "kind": "classifier_preprocessed",
            "feature_columns": feature_columns,
            "preprocessor": preprocessor,
            "model": lgbm_classifier,
        },
        args.output_dir / "q2_lightgbm_classifier_ranker.joblib",
    )

    rank_train, rank_train_matrix, train_groups = _rank_sort(train, train_matrix)
    rank_validation, rank_validation_matrix, validation_groups = _rank_sort(
        validation, validation_matrix
    )
    ranker = LGBMRanker(
        objective="lambdarank",
        metric="map",
        eval_at=(10, 25, 50),
        n_estimators=400,
        learning_rate=0.03,
        num_leaves=15,
        max_depth=5,
        min_child_samples=100,
        reg_lambda=2.0,
        colsample_bytree=0.8,
        random_state=20260816,
        n_jobs=-1,
        verbosity=-1,
    )
    ranker.fit(
        rank_train_matrix,
        rank_train["target"],
        group=train_groups,
        eval_set=[(rank_validation_matrix, rank_validation["target"])],
        eval_group=[validation_groups],
        callbacks=[early_stopping(30, verbose=False), log_evaluation(0)],
    )
    scores["lightgbm_ranker"] = {
        "validation": ranker.predict(validation_matrix),
        "test": ranker.predict(test_matrix),
    }
    joblib.dump(
        {
            "kind": "ranker_preprocessed",
            "feature_columns": feature_columns,
            "preprocessor": preprocessor,
            "model": ranker,
        },
        args.output_dir / "q2_lightgbm_ranker.joblib",
    )

    feature_names = preprocessor.get_feature_names_out()
    classifier_gain = _importance(
        preprocessor,
        lgbm_classifier.booster_.feature_importance(importance_type="gain"),
        kind="classifier_gain",
    )
    classifier_split = _importance(
        preprocessor,
        lgbm_classifier.booster_.feature_importance(importance_type="split"),
        kind="classifier_split",
    )
    ranker_gain = _importance(
        preprocessor,
        ranker.booster_.feature_importance(importance_type="gain"),
        kind="ranker_gain",
    )
    pd.concat([classifier_gain, classifier_split, ranker_gain]).to_csv(
        args.output_dir / "lightgbm_feature_importance.csv", index=False
    )

    metrics: dict[str, dict[str, object]] = {}
    for model_name, by_split in scores.items():
        metrics[model_name] = {}
        for split, part in (("validation", validation), ("test", test)):
            metrics[model_name][split] = _score_metrics(part, by_split[split], ks=ks)

    best_lgbm = max(
        ("lightgbm_classifier", "lightgbm_ranker"),
        key=lambda name: metrics[name]["validation"]["pr_auc"],
    )
    for split, part in (("validation", validation), ("test", test)):
        geometry_percentile = _percentile_within_group(part, scores["geometry"][split])
        ml_percentile = _percentile_within_group(part, scores[best_lgbm][split])
        scores.setdefault("hybrid", {})[split] = 0.25 * geometry_percentile + 0.75 * ml_percentile
        metrics.setdefault("hybrid", {})[split] = _score_metrics(
            part, scores["hybrid"][split], ks=ks
        )

    hard_metrics: dict[str, object] = {}
    for split, part in (("validation", validation), ("test", test)):
        hard_mask = part["candidate_source"].isin(["GEOMETRY_TOP", "GEOMETRY_MID"])
        hard = part[hard_mask].copy()
        hard_metrics[split] = {
            name: _score_metrics(hard, by_split[split][hard_mask.to_numpy()], ks=ks)
            for name, by_split in scores.items()
        }

    result = {
        "split_rows": {
            split: {
                "rows": int(len(part)),
                "positives": int(part["target"].sum()),
                "novel_positives": int((part["target"].eq(1) & part["novel"]).sum()),
            }
            for split, part in (("train", train), ("validation", validation), ("test", test))
        },
        "k_values": list(ks),
        "metrics": metrics,
        "hard_geometry_metrics": hard_metrics,
        "best_lightgbm_by_validation_pr_auc": best_lgbm,
        "lightgbm_classifier_best_iteration": int(lgbm_classifier.best_iteration_),
        "lightgbm_ranker_best_iteration": int(ranker.best_iteration_),
        "hybrid_weights": {"geometry": 0.25, "ml": 0.75},
    }
    (args.output_dir / "offline_metrics.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    rows = []
    for model, split_values in metrics.items():
        for split, values in split_values.items():
            for k, top in values["top_k"].items():
                rows.append(
                    {
                        "model": model,
                        "split": split,
                        "pr_auc": values["pr_auc"],
                        "roc_auc": values["roc_auc"],
                        "brier": values["brier"],
                        "k": int(k),
                        **top,
                    }
                )
    pd.DataFrame(rows).to_csv(args.output_dir / "offline_ranking.csv", index=False)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
