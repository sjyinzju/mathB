"""Run-scoped shared cache for static route physics and route reconstruction.

Lifecycle: one ``SolverCache`` is created per solver run (or per process
segment such as B0 -> Savings -> relocation -> ejection) and passed to every
operator, so static work computed in one stage is reused by all later stages.
The object owns its state; there is no global mutable singleton, which keeps
multi-seed / multi-start runs isolatable by simply creating one cache per run.

What is cached (all assignment-independent static physics):

- ``augmentation``: technical-stop search results keyed by
  ``(base_airport, aircraft_type, ordered_service_nodes)``. The search never
  sees passengers or loads, so this key fully determines the result.
- ``skeleton``: best ``(aircraft_type, stops, service_order)`` rebuild for a
  route keyed by ``(secondary_order, base_airport, od_count_signature)``.
  The signature records exact (origin, destination, count) multiset; per-route
  passenger evaluations are still recomputed on every hit.
- ``lower_bound`` / ``direct_time``: static time lower bounds, same signature
  logic.

Never cached here: any full ``RouteEvaluation`` or passenger travel time,
because those depend on the concrete assignment/load profile.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Tuple

from .models import AugmentationResult
from .physics import LegPhysics
from .technical_stops import augment_service_sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .data import ProblemData


AugmentationKey = Tuple[str, str, Tuple[str, ...]]
SkeletonKey = Tuple[Tuple[str, ...], str, Tuple[Tuple[str, str, int], ...]]
Signature = Tuple[Tuple[str, str, int], ...]


class SolverCache:
    """Shared, hit/miss-instrumented cache for static route computations."""

    def __init__(self, data: "ProblemData") -> None:
        self.data = data
        self.physics = LegPhysics(data.config, data.matrix)
        self.augmentation: Dict[AugmentationKey, AugmentationResult] = {}
        self.skeleton: Dict[SkeletonKey, Tuple[str, tuple, Tuple[str, ...]] | None] = {}
        self.lower_bound: Dict[Tuple[str, Signature], int] = {}
        self.direct_time: Dict[AugmentationKey, int] = {}
        self._counters = {
            "augmentation_hits": 0,
            "augmentation_misses": 0,
            "skeleton_hits": 0,
            "skeleton_misses": 0,
            "lower_bound_hits": 0,
            "lower_bound_misses": 0,
            "direct_time_hits": 0,
            "direct_time_misses": 0,
        }

    # -- technical-stop augmentation -------------------------------------
    def augmentation_result(
        self,
        base_airport: str,
        aircraft_type: str,
        service_order: Tuple[str, ...],
        *,
        stop_limit: int | None = None,
        candidate_nodes=None,
    ) -> AugmentationResult:
        """Cached ``augment_service_sequence``; key fully determines the result."""
        key: AugmentationKey = (base_airport, aircraft_type, service_order)
        cached = self.augmentation.get(key)
        if cached is not None:
            self._counters["augmentation_hits"] += 1
            return cached
        self._counters["augmentation_misses"] += 1
        result = augment_service_sequence(
            base_airport,
            aircraft_type,
            service_order,
            matrix=self.data.matrix,
            config=self.data.config,
            physics=self.physics,
            stop_limit=stop_limit,
            candidate_nodes=candidate_nodes,
        )
        self.augmentation[key] = result
        return result

    # -- bookkeeping -------------------------------------------------------
    def hit(self, name: str) -> None:
        self._counters[f"{name}_hits"] += 1

    def miss(self, name: str) -> None:
        self._counters[f"{name}_misses"] += 1

    def clear(self) -> None:
        """Drop cached results (counters preserved); results stay reproducible."""
        self.augmentation.clear()
        self.skeleton.clear()
        self.lower_bound.clear()
        self.direct_time.clear()

    def stats(self) -> Dict[str, int]:
        return {
            **self._counters,
            "augmentation_entries": len(self.augmentation),
            "skeleton_entries": len(self.skeleton),
            "lower_bound_entries": len(self.lower_bound),
            "direct_time_entries": len(self.direct_time),
            "leg_physics_entries": self.physics.entries(),
        }
