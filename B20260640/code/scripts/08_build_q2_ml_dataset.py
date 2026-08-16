from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.solver.q2_learning import build_q2_learning_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Build leakage-safe Q2 candidate dataset")
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "outputs" / "q2" / "ml-data"
    )
    args = parser.parse_args()
    diagnostics = build_q2_learning_dataset(args.run_dirs, args.output_dir)
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
