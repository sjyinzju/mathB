from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.validation import validate_solution


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate official-format Problem B solution CSV files")
    parser.add_argument("--question", required=True, choices=["q1", "q2", "q3"])
    parser.add_argument("--routes", required=True, type=Path)
    parser.add_argument("--assignments", required=True, type=Path)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "raw")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()
    result = validate_solution(
        args.question,
        args.routes,
        args.assignments,
        data_dir=args.data_dir,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print("VALID" if result.valid else "INVALID")
        if result.metrics:
            print(json.dumps(result.metrics.to_dict(), ensure_ascii=False, indent=2))
        for issue in result.issues:
            print(str(issue))
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
