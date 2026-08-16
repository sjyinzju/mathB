from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .cache import SolverCache
from .data import ProblemData
from .models import Solution
from .q2_lns import (
    Q2LnsConfig,
    Q2LocalRepair,
    exact_q2_elite_recombination,
    q2_solution_diversity,
)


@dataclass(frozen=True)
class Q2EliteEntry:
    solution_id: str
    solution: Solution
    source: str


class Q2ElitePool:
    """Compact quality-and-diversity archive for Q2 restarts/recombination."""

    def __init__(
        self,
        *,
        max_size: int = 6,
        min_diversity: float = 0.02,
        quality_slack_minutes: int = 80,
    ) -> None:
        if max_size < 2 or min_diversity < 0 or quality_slack_minutes < 0:
            raise ValueError("invalid elite pool thresholds")
        self.max_size = max_size
        self.min_diversity = min_diversity
        self.quality_slack_minutes = quality_slack_minutes
        self._entries: list[Q2EliteEntry] = []

    @property
    def entries(self) -> tuple[Q2EliteEntry, ...]:
        return tuple(self._entries)

    def promote(self, entry: Q2EliteEntry) -> bool:
        if any(item.solution_id == entry.solution_id for item in self._entries):
            return False
        if not self._entries:
            self._entries.append(entry)
            return True
        best_minutes = min(
            item.solution.metrics.total_aircraft_time_minutes for item in self._entries
        )
        if (
            entry.solution.metrics.total_aircraft_time_minutes
            > best_minutes + self.quality_slack_minutes
        ):
            return False
        nearest = min(
            q2_solution_diversity(entry.solution, item.solution)
            for item in self._entries
        )
        if nearest < self.min_diversity:
            nearest_entry = min(
                self._entries,
                key=lambda item: q2_solution_diversity(entry.solution, item.solution),
            )
            if (
                entry.solution.metrics.comparison_key()
                >= nearest_entry.solution.metrics.comparison_key()
            ):
                return False
            self._entries.remove(nearest_entry)
        self._entries.append(entry)
        self._trim()
        return entry in self._entries

    def _trim(self) -> None:
        while len(self._entries) > self.max_size:
            best = min(
                self._entries, key=lambda item: item.solution.metrics.comparison_key()
            )
            removable = [item for item in self._entries if item is not best]
            victim = min(
                removable,
                key=lambda item: (
                    min(
                        q2_solution_diversity(item.solution, other.solution)
                        for other in self._entries
                        if other is not item
                    ),
                    -item.solution.metrics.total_aircraft_time_minutes,
                    item.solution_id,
                ),
            )
            self._entries.remove(victim)

    def select_partner(
        self,
        current: Solution,
        *,
        diversity_aware: bool,
    ) -> Q2EliteEntry:
        if not self._entries:
            raise ValueError("elite pool is empty")
        if not diversity_aware:
            return min(
                self._entries, key=lambda item: item.solution.metrics.comparison_key()
            )
        best_minutes = min(
            item.solution.metrics.total_aircraft_time_minutes for item in self._entries
        )
        return max(
            self._entries,
            key=lambda item: (
                q2_solution_diversity(current, item.solution),
                -(item.solution.metrics.total_aircraft_time_minutes - best_minutes),
                item.solution_id,
            ),
        )


def q2_difference_path_relink(
    current: Solution,
    partner: Solution,
    data: ProblemData,
    *,
    cache: SolverCache,
    config: Q2LnsConfig,
    steps: int = 3,
) -> tuple[Solution, tuple[dict[str, object], ...]]:
    """Small exact difference path; each step uses another difference region."""
    best = current
    logs: list[dict[str, object]] = []
    for step in range(max(0, steps)):
        repair = exact_q2_elite_recombination(
            best,
            partner,
            data,
            cache=cache,
            config=config,
            iteration=step,
        )
        candidate = repair.solution
        accepted = bool(
            candidate is not None
            and candidate.metrics.comparison_key() < best.metrics.comparison_key()
        )
        logs.append(
            {
                "step": step,
                "accepted": accepted,
                "before": best.metrics.total_aircraft_time_minutes,
                "after": (
                    candidate.metrics.total_aircraft_time_minutes
                    if candidate is not None
                    else None
                ),
                "diversity_to_partner": q2_solution_diversity(best, partner),
                "neighborhood": repair.diagnostics.get("elite_neighborhood", []),
                "runtime_seconds": repair.diagnostics.get("runtime_seconds", 0.0),
            }
        )
        if accepted and candidate is not None:
            best = candidate
    return best, tuple(logs)


def q2_local_branching_feasibility() -> dict[str, object]:
    """Document the bounded experiment gate without inventing a second master."""
    return {
        "feasible_without_master_refactor": False,
        "decision": "REJECT",
        "reason": (
            "The current aggregated restricted master exposes route variants and OD "
            "allocation, but not stable incumbent binary identities across regenerated "
            "local pools. A Hamming constraint would therefore require changing the "
            "shared master representation rather than a bounded local experiment."
        ),
        "existing_safe_equivalent": (
            "Fix-and-optimize keeps all routes outside a structured exact-repair window "
            "fixed and lets the existing restricted local master optimize the window."
        ),
    }


def elite_pair_diagnostics(entries: Iterable[Q2EliteEntry]) -> list[dict[str, object]]:
    values = tuple(entries)
    return [
        {
            "left": left.solution_id,
            "right": right.solution_id,
            "left_aircraft_minutes": left.solution.metrics.total_aircraft_time_minutes,
            "right_aircraft_minutes": right.solution.metrics.total_aircraft_time_minutes,
            "solution_distance": q2_solution_diversity(left.solution, right.solution),
        }
        for left_index, left in enumerate(values)
        for right in values[left_index + 1 :]
    ]
