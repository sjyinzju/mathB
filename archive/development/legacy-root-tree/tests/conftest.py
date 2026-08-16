from __future__ import annotations

from pathlib import Path

import pytest

from src.config import ROOT, load_config
from src.io_utils import write_csv


@pytest.fixture(scope="session")
def config():
    return load_config()


@pytest.fixture(scope="session")
def raw_dir() -> Path:
    return ROOT / "data" / "raw"


@pytest.fixture
def small_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    nodes = ["A01", "F006", "F020", "F014"]
    distances = {
        "A01": {"A01": 0, "F006": 235, "F020": 171, "F014": 164},
        "F006": {"A01": 235, "F006": 0, "F020": 168, "F014": 80},
        "F020": {"A01": 171, "F006": 168, "F020": 0, "F014": 30},
        "F014": {"A01": 164, "F006": 80, "F020": 30, "F014": 0},
    }
    write_csv(
        data_dir / "distances.csv",
        ["from_id", *nodes],
        ({"from_id": origin, **distances[origin]} for origin in nodes),
    )
    return data_dir
