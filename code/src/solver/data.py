from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..config import ProblemConfig, ROOT, load_config
from ..data_pipeline import load_distance_matrix
from ..io_utils import read_csv
from .models import DemandPool


@dataclass(frozen=True)
class ProblemData:
    config: ProblemConfig
    matrix: dict[str, dict[str, float]]
    q1_pools: dict[tuple[str, str], DemandPool]
    q2_pools: dict[tuple[str, str], DemandPool]

    @property
    def q1_passenger_count(self) -> int:
        return sum(pool.quantity for pool in self.q1_pools.values())

    @property
    def q2_passenger_count(self) -> int:
        return sum(pool.quantity for pool in self.q2_pools.values())


def load_problem_data(
    *,
    raw_dir: Path | str | None = None,
    processed_dir: Path | str | None = None,
    config: ProblemConfig | None = None,
) -> ProblemData:
    config = config or load_config()
    raw_dir = Path(raw_dir) if raw_dir else ROOT / "data" / "raw"
    processed_dir = Path(processed_dir) if processed_dir else ROOT / "data" / "processed"
    _, matrix = load_distance_matrix(raw_dir / "distances.csv")
    demand_path = processed_dir / "demands_q1.csv"
    rows = read_csv(demand_path)
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        origin = row["origin"]
        destination = row["destination"]
        if origin != "LAND" and origin not in config.airports:
            raise ValueError(f"Q1 origin must be LAND or an airport, got {origin}")
        if destination not in config.facilities:
            raise ValueError(f"Q1 destination must be a sea facility, got {destination}")
        grouped[(origin, destination)].append(row["person_id"])
    q1_pools = {
        key: DemandPool(key[0], key[1], tuple(sorted(person_ids)))
        for key, person_ids in grouped.items()
    }

    q2_rows = read_csv(processed_dir / "demands_q2.csv")
    q2_grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    valid_nodes = set(config.airports) | set(config.facilities) | {"LAND"}
    for row in q2_rows:
        origin = row["origin"]
        destination = row["destination"]
        if origin not in valid_nodes or destination not in valid_nodes:
            raise ValueError(f"Unknown Q2 endpoint: {origin}->{destination}")
        if origin == destination:
            raise ValueError(f"Q2 demand must have distinct endpoints: {origin}")
        if origin in config.airports and destination in config.airports:
            raise ValueError(f"Q2 demand cannot connect two airports: {origin}->{destination}")
        q2_grouped[(origin, destination)].append(row["person_id"])
    q2_pools = {
        key: DemandPool(key[0], key[1], tuple(sorted(person_ids)))
        for key, person_ids in q2_grouped.items()
    }
    return ProblemData(
        config=config,
        matrix=matrix,
        q1_pools=q1_pools,
        q2_pools=q2_pools,
    )
