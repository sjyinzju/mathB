from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


@dataclass(frozen=True)
class Q2MLRanker:
    """Read-only pre-exact candidate ranker.

    The serialized artifact owns preprocessing and the fitted estimator.  This
    adapter deliberately exposes only scores: route generation, exact variant
    construction, local MILP feasibility and acceptance remain authoritative.
    """

    artifact_path: Path
    kind: str
    feature_columns: tuple[str, ...]
    payload: object

    @classmethod
    def load(cls, path: Path) -> "Q2MLRanker":
        import joblib

        resolved = path.resolve()
        artifact = joblib.load(resolved)
        required = {"kind", "feature_columns", "model"}
        missing = required - set(artifact)
        if missing:
            raise ValueError(f"Invalid Q2 ML artifact; missing {sorted(missing)}")
        return cls(
            artifact_path=resolved,
            kind=str(artifact["kind"]),
            feature_columns=tuple(str(value) for value in artifact["feature_columns"]),
            payload=artifact,
        )

    def score_rows(self, rows: Iterable[Mapping[str, object]]) -> list[float]:
        import pandas as pd

        frame = pd.DataFrame(list(rows)).reindex(columns=self.feature_columns)
        artifact = self.payload
        if not isinstance(artifact, dict):
            raise TypeError("Invalid Q2 ML artifact payload")
        model = artifact["model"]
        if self.kind == "classifier":
            values = model.predict_proba(frame)[:, 1]
        else:
            preprocessor = artifact.get("preprocessor")
            if preprocessor is None:
                raise ValueError("Preprocessed Q2 ML artifact lacks preprocessor")
            matrix = preprocessor.transform(frame)
            if self.kind == "classifier_preprocessed":
                values = model.predict_proba(matrix)[:, 1]
            elif self.kind == "ranker_preprocessed":
                values = model.predict(matrix)
            else:
                raise ValueError(f"Unsupported Q2 ML ranker kind: {self.kind}")
        return [float(value) for value in values]
