"""Static per-leg physics lookup shared by all Q1/Q2/Q3 solver layers.

The table is built once per problem instance from the distance matrix and
aircraft configuration. Values are produced by the exact same Stage 1 rules
(``src.rules.flight_minutes`` / ``src.rules.fuel_for_leg``), so the lookup is
bit-identical to recomputing the formulas; it only removes repeated work.

The layer is deliberately assignment-free: it knows nothing about passengers,
loads, time windows or aircraft identities. Only geometry, flight minutes,
fuel burn and reserves.
"""
from __future__ import annotations

from typing import Dict, Tuple

from ..config import ProblemConfig
from ..rules import flight_minutes as _rule_flight_minutes
from ..rules import fuel_for_leg as _rule_fuel_for_leg


class LegPhysics:
    """O(1) lookup of distance / ceil flight minutes / fuel burn per leg."""

    __slots__ = ("_config", "_matrix", "_records")

    def __init__(self, config: ProblemConfig, matrix: Dict[str, Dict[str, float]]) -> None:
        self._config = config
        self._matrix = matrix
        records: Dict[str, Dict[Tuple[str, str], Tuple[float, int, float]]] = {}
        for aircraft_type, aircraft in config.aircraft_types.items():
            table: Dict[Tuple[str, str], Tuple[float, int, float]] = {}
            for origin, row in matrix.items():
                for destination, distance in row.items():
                    table[(origin, destination)] = (
                        distance,
                        _rule_flight_minutes(distance, aircraft.speed_kmh),
                        _rule_fuel_for_leg(distance, aircraft),
                    )
            records[aircraft_type] = table
        self._records = records

    @property
    def aircraft_types(self) -> tuple[str, ...]:
        return tuple(self._records)

    def entries(self) -> int:
        return sum(len(table) for table in self._records.values())

    def leg(self, aircraft_type: str, origin: str, destination: str) -> Tuple[float, int, float]:
        """Return ``(distance_km, flight_minutes, fuel_kg)`` for one leg.

        Raises ``KeyError`` exactly like a raw ``matrix[origin][destination]``
        access when the pair has no distance entry.
        """
        return self._records[aircraft_type][(origin, destination)]

    def flight_minutes(self, aircraft_type: str, origin: str, destination: str) -> int:
        return self._records[aircraft_type][(origin, destination)][1]

    def fuel_for_leg(self, aircraft_type: str, origin: str, destination: str) -> float:
        return self._records[aircraft_type][(origin, destination)][2]

    def distance(self, origin: str, destination: str) -> float:
        return self._matrix[origin][destination]

    def table_for(self, aircraft_type: str) -> Dict[Tuple[str, str], Tuple[float, int, float]]:
        """Raw per-aircraft lookup table for tight inner loops."""
        return self._records[aircraft_type]
