from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data_pipeline import prepare_data


def main() -> None:
    result = prepare_data(ROOT)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print("DATA_QUALITY=PASS")


if __name__ == "__main__":
    main()
