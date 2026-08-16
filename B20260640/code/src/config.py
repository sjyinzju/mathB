from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "configs" / "problem.json"


@dataclass(frozen=True)
class AircraftType:
    aircraft_type: str
    seats: int
    speed_kmh: float
    burn_kg_per_km: float
    tank_capacity_kg: float
    reserve_kg: float

    @property
    def usable_fuel_kg(self) -> float:
        return self.tank_capacity_kg - self.reserve_kg

    @property
    def full_tank_usable_distance_km(self) -> float:
        return self.usable_fuel_kg / self.burn_kg_per_km


@dataclass(frozen=True)
class ProblemConfig:
    airports: tuple[str, ...]
    facilities: tuple[str, ...]
    refuel_facilities: frozenset[str]
    max_sea_landings: int
    stop_without_refuel_minutes: int
    stop_with_refuel_minutes: int
    aircraft_types: dict[str, AircraftType]
    planning_start: datetime
    planning_end: datetime
    earliest_departure: time
    latest_departure: time
    latest_return: time
    turnaround_minutes: int
    fleet_counts: dict[str, dict[str, int]]
    task_priority: dict[str, int]
    mandatory_task_types: frozenset[str]
    optional_task_types: frozenset[str]

    @property
    def nodes(self) -> tuple[str, ...]:
        return self.airports + self.facilities

    @property
    def fleet_ids(self) -> tuple[str, ...]:
        ids: list[str] = []
        for airport in self.airports:
            for aircraft_type in self.aircraft_types:
                count = self.fleet_counts[airport][aircraft_type]
                ids.extend(f"{airport}-{aircraft_type}-H{index:02d}" for index in range(1, count + 1))
        return tuple(ids)

    def aircraft_home_and_type(self, aircraft_id: str) -> tuple[str, str] | None:
        if aircraft_id not in self.fleet_ids:
            return None
        airport, aircraft_type, _ = aircraft_id.split("-")
        return airport, aircraft_type


def _parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> ProblemConfig:
    path = Path(path)
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    aircraft = {
        key: AircraftType(aircraft_type=key, **values)
        for key, values in raw["aircraft_types"].items()
    }
    q3 = raw["q3"]
    config = ProblemConfig(
        airports=tuple(raw["airports"]),
        facilities=tuple(raw["facilities"]),
        refuel_facilities=frozenset(raw["refuel_facilities"]),
        max_sea_landings=int(raw["max_sea_landings"]),
        stop_without_refuel_minutes=int(raw["minimum_stop_minutes"]["without_refuel"]),
        stop_with_refuel_minutes=int(raw["minimum_stop_minutes"]["with_refuel"]),
        aircraft_types=aircraft,
        planning_start=datetime.strptime(q3["planning_start"], "%Y-%m-%d %H:%M"),
        planning_end=datetime.strptime(q3["planning_end"], "%Y-%m-%d %H:%M"),
        earliest_departure=_parse_time(q3["earliest_departure"]),
        latest_departure=_parse_time(q3["latest_departure"]),
        latest_return=_parse_time(q3["latest_return"]),
        turnaround_minutes=int(q3["turnaround_minutes"]),
        fleet_counts={airport: {k: int(v) for k, v in counts.items()} for airport, counts in q3["fleet_counts"].items()},
        task_priority={key: int(value) for key, value in q3["task_priority"].items()},
        mandatory_task_types=frozenset(q3["mandatory_task_types"]),
        optional_task_types=frozenset(q3["optional_task_types"]),
    )
    _validate_config(config)
    return config


def _validate_config(config: ProblemConfig) -> None:
    if len(config.airports) != 3 or len(config.facilities) != 52:
        raise ValueError("Configuration must contain exactly 3 airports and 52 facilities")
    if len(set(config.nodes)) != 55:
        raise ValueError("Configured node identifiers must be unique")
    if not config.refuel_facilities <= set(config.facilities):
        raise ValueError("Every refuel node must be a sea facility")
    if len(config.fleet_ids) != 24:
        raise ValueError("Q3 fleet configuration must expand to exactly 24 aircraft")
    expected_tasks = config.mandatory_task_types | config.optional_task_types
    if expected_tasks != set(config.task_priority):
        raise ValueError("Task priority keys and task type sets disagree")
