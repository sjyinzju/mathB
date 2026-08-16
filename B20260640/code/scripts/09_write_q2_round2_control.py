from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

import scipy

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import sha256, write_json


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _artifact(directory: Path) -> dict[str, object]:
    metrics = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
    return {
        "directory": str(directory.relative_to(ROOT)).replace("\\", "/"),
        "routes_sha256": sha256(directory / "q2-routes.csv"),
        "assignments_sha256": sha256(directory / "q2-assignments.csv"),
        "validator_sha256": sha256(directory / "q2-validator.json"),
        "metrics": metrics["validator_metrics"],
        "validator_pass": bool(metrics["gate_pass"] and metrics["metrics_match"]),
    }


def main() -> int:
    round1 = ROOT / "outputs" / "q2" / "runs" / "20260815-q2-final-repro-s2"
    extended = [
        ROOT / "outputs" / "q2" / "runs" / "20260815-q2-round2-extended-control-s5",
        ROOT / "outputs" / "q2" / "runs" / "20260815-q2-round2-extended-control-s6",
    ]
    round1_config = json.loads((round1 / "run_config.json").read_text(encoding="utf-8"))
    payload = {
        "manifest_version": 1,
        "git": {
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("branch", "--show-current"),
            "tracked_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
            "worktree_dirty": bool(_git("status", "--porcelain")),
        },
        "environment": {
            "python": platform.python_version(),
            "scipy": scipy.__version__,
            "backend": "scipy.optimize.milp/HiGHS",
        },
        "round1_control": {
            **_artifact(round1),
            "config": round1_config["config"],
            "seed_set": [0, 1, 2, 3, 4],
            "formal_aircraft_minutes": [18010, 18048, 17958, 18043, 18102],
        },
        "round2_absolute_control": {
            "algorithm_change": False,
            "wall_seconds_per_restart": 180,
            "runs": [_artifact(directory) for directory in extended],
            "winning_run": str(extended[0].relative_to(ROOT)).replace("\\", "/"),
        },
        "input_hashes": {
            "raw_files": {
                str(path.relative_to(ROOT)).replace("\\", "/"): sha256(path)
                for path in sorted((ROOT / "data" / "raw").glob("*"))
                if path.is_file()
            }
        },
        "bound_scope": "restricted_local_master",
        "notes": [
            "Round1 immutable control is preserved and was not overwritten.",
            "Extended controls start from the same validated 17,958 solution.",
            "No ML model is trained in Round2.",
        ],
    }
    write_json(ROOT / "ROUND2_CONTROL_MANIFEST.json", payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
