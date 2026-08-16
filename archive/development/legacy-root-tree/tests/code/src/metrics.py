from __future__ import annotations

from pathlib import Path

from .validation import Metrics, validate_solution


def score_solution(
    question: str,
    routes_path: Path | str,
    assignments_path: Path | str,
    *,
    data_dir: Path | str | None = None,
) -> Metrics:
    """Validate a submitted solution and return the five unified competition metrics."""
    result = validate_solution(
        question,
        routes_path,
        assignments_path,
        data_dir=data_dir,
    )
    if not result.valid:
        summary = "; ".join(str(issue) for issue in result.issues[:10])
        raise ValueError(f"Cannot score invalid {question} solution: {summary}")
    assert result.metrics is not None
    return result.metrics
