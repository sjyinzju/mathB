"""Certified fast exact pricing oracle for Q1 branch-and-price subproblems.

This module solves exactly the same allocated-sortie pricing subproblem as
``q1_pricing.exact_pricing`` (the permanent HiGHS MILP reference oracle)
with a layered label-setting dynamic program instead of a position-indexed
MILP. It is certificate-equivalent: same minimum reduced cost, and a winner
column whose shared-evaluator recomputation agrees with the internal DP
accounting to 5e-6.

State and exactness argument (full proof in Q1_FAST_PRICING_REPORT.md):

* State after ``depth`` sea landings: facility node, fuel level, accumulated
  objective cost, true clock, and the visited set of demand-bearing
  destinations. Because refuelling always fills to tank capacity, fuel is
  fully determined by the refuel decision chain; the visited set drives the
  allocation reward, which depends on visited destinations only through the
  OR of visit indicators (repeats add no new demand).
* Reward signature: the allocation reward of a visited set equals the sum of
  the positive values among the ``seats`` best demand-unit duals of the
  visited destinations (or the single best unit when nothing is positive).
  Each label carries that top-``seats`` value tuple, merged exactly once per
  first visit; membership in the visited set decides whether a landing
  merges new units, so repeats never double-count demand.
* Dominance (exact): within identical ``(depth, node, visited set)`` a label
  with cost_a <= cost_b, fuel_a >= fuel_b and clock_a <= clock_b dominates
  label b: both have identical future reward structure, and every
  leg/refuel/termination decision feasible from b is feasible from a at no
  larger cost, no smaller fuel and no later clock (the clock condition keeps
  the duration tie-break among equal reduced costs intact). Visited sets
  must match exactly; the reward of a continuation depends on which
  destinations were already visited (revisits add no units), so signature-
  only dominance would be unsound.
* Bound pruning (exact): any kept label still needs at least one leg (an
  inter-facility leg or the return leg), so ``cost + leg_min - reward_bound``
  lower-bounds the completed reduced cost; labels reaching the incumbent
  bound cannot improve it. The reward bound is per-label and layer-aware:
  a label at landing depth ``d`` can first-visit at most ``K = landings - d``
  more destinations, and the top-``seats`` positive sum is subadditive over
  destination unit multisets, so the final reward is at most the label's own
  signature positive sum plus the sum of the ``K`` largest per-destination
  positive top-``seats`` sums (precomputed prefix), further capped by the
  global top-``seats`` bound; the minimum of two valid upper bounds is
  valid. An optional ``initial_incumbent_rc`` (the reduced cost of any
  known feasible column under the same duals) may seed the incumbent;
  seeding only strengthens pruning and can never remove a column better
  than the seed.
* The winner is re-evaluated by the shared ``evaluate_route`` and
  ``branch_column_reduced_cost`` machinery; disagreement beyond 5e-6 or a
  duration mismatch raises ``RuntimeError``, so silent accounting drift can
  never certify.

No beam, no top-K, no heuristic pruning, no approximate dominance: every
pruned partial route is dominated in the proven sense above.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping

import numpy as np

from ..rules import EPSILON
from .data import ProblemData
from .evaluator import evaluate_route
from .models import RoutePlan, RouteStop
from .physics import LegPhysics
from .q1_pricing import (
    PRICING_TOL,
    ArcBranchRow,
    ExactPricingResult,
    ExactRouteColumn,
    branch_column_reduced_cost,
)


_CROSS_CHECK_TOL = 5.0e-6
_TIE_TOL = 1.0e-9


@dataclass(frozen=True)
class FastPricingDiagnostics:
    labels_created: int
    labels_kept: int
    dominance_prunes: int
    bound_prunes: int
    terminated_routes: int
    incumbent_updates: int
    layers: int
    reward_max: float
    elapsed_seconds: float


class _LayerFrontier:
    """Exact Pareto frontier of labels for one (node, visited set) state.

    Kept sorted by accumulated reduced-cost contribution ascending. Built by
    :func:`_pareto_filter`, which keeps exactly the undominated labels of a
    candidate batch. Dominance compares cost (lower is better), fuel (higher
    is better) and clock (lower is better): the clock is required so that
    dominance never destroys the duration tie-break among equal reduced
    costs, keeping the winner canonical with the MILP oracle.
    """

    __slots__ = ("costs", "fuels", "times", "sigs", "paths", "flags")

    def __init__(self) -> None:
        self.costs: list[float] = []
        self.fuels: list[float] = []
        self.times: list[int] = []
        self.sigs: list[tuple[float, ...]] = []
        self.paths: list[tuple[int, ...]] = []
        self.flags: list[tuple[bool, ...]] = []

    def __len__(self) -> int:
        return len(self.costs)


def _pareto_filter(
    raw: list[tuple],
) -> tuple[_LayerFrontier, int]:
    """Exact Pareto filter of one state key's candidate labels.

    Returns the undominated labels (sorted by cost ascending) and the number
    of dominance-pruned candidates. Label ``j`` dominates label ``i`` iff
    cost_j <= cost_i, fuel_j >= fuel_i and clock_j <= clock_i.

    Processing labels in (cost ascending, fuel descending, clock ascending)
    order places every candidate dominator of the current label strictly
    before it. The already-processed labels are summarised by their exact
    two-dimensional Pareto frontier over (fuel higher is better, clock
    lower is better), kept strictly decreasing in both coordinates; the
    current label is dominated iff that frontier contains a point with fuel
    >= its fuel and clock <= its clock, which the monotone frontier answers
    with a single binary search. The filter is therefore exactly the set of
    three-dimensionally undominated labels, with no heuristic removal.
    """

    total = len(raw)
    frontier = _LayerFrontier()
    if total == 1:
        (cost, fuel, clock, sig, path, refuels) = raw[0]
        frontier.costs = [cost]
        frontier.fuels = [fuel]
        frontier.times = [clock]
        frontier.sigs = [sig]
        frontier.paths = [path]
        frontier.flags = [refuels]
        return frontier, 0
    costs = np.array([label[0] for label in raw], dtype=float)
    fuels = np.array([label[1] for label in raw], dtype=float)
    clocks = np.array([label[2] for label in raw], dtype=np.int64)
    order = np.lexsort((clocks, -fuels, costs))
    keep = np.zeros(total, dtype=bool)
    # 2D frontier of processed labels: front_fuels strictly decreasing and
    # front_clocks strictly decreasing at the same positions (a pair with
    # larger fuel and smaller clock would dominate the other). The frontier
    # is dominance-equivalent to every processed label: a removed point is
    # dominated by a frontier point, so any query it could answer is also
    # answered by its dominator.
    front_fuels: list[float] = []
    front_clocks: list[int] = []
    for pos in order:
        fuel = float(fuels[pos])
        clock = int(clocks[pos])
        # Points with fuel >= fuel are the prefix [0, k); front_clocks is
        # decreasing, so their minimum clock is the last one of the prefix.
        k = _first_lt(front_fuels, fuel)
        if k and front_clocks[k - 1] <= clock:
            continue
        keep[pos] = True
        # Insert (fuel, clock) and drop the contiguous run it dominates:
        # points with fuel <= fuel (indices >= lo) and clock >= clock
        # (indices < m). Equal-fuel or equal-clock survivors are impossible
        # here (they would dominate, or be dominated by, the new point), so
        # both sequences stay strictly decreasing.
        lo = _first_le(front_fuels, fuel)
        m = _first_lt(front_clocks, clock)
        if m < lo:
            m = lo
        front_fuels[lo:m] = [fuel]
        front_clocks[lo:m] = [clock]
    kept_positions = order[np.flatnonzero(keep[order])]
    frontier.costs = [float(costs[pos]) for pos in kept_positions]
    frontier.fuels = [float(fuels[pos]) for pos in kept_positions]
    frontier.times = [int(clocks[pos]) for pos in kept_positions]
    frontier.sigs = [raw[pos][3] for pos in kept_positions]
    frontier.paths = [raw[pos][4] for pos in kept_positions]
    frontier.flags = [raw[pos][5] for pos in kept_positions]
    return frontier, total - int(keep.sum())


def _first_le(values: list, target) -> int:
    """First index with values[i] <= target in a strictly decreasing list."""

    lo, hi = 0, len(values)
    while lo < hi:
        mid = (lo + hi) // 2
        if values[mid] > target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _first_lt(values: list, target) -> int:
    """First index with values[i] < target in a strictly decreasing list."""

    lo, hi = 0, len(values)
    while lo < hi:
        mid = (lo + hi) // 2
        if values[mid] >= target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _build_arc_arrays(
    data: ProblemData,
    base_airport: str,
    aircraft_type: str,
    nodes: tuple[str, ...],
    physics: LegPhysics,
    branch_duals: Mapping[ArcBranchRow, float],
    route_cost_multiplier: float,
) -> dict[str, np.ndarray]:
    """Vectorised per-leg minutes / burn / traversal-cost lookups.

    ``cost_*`` carries the official integer objective scaled by
    ``route_cost_multiplier`` plus the additive branch-dual traversal term
    (``-dual * canonical_sign`` per traversal, identical to the MILP). The
    ``minutes_*`` arrays stay unscaled so true duration is tracked
    independently of branch terms.
    """

    node_count = len(nodes)
    index = {node: i for i, node in enumerate(nodes)}
    minutes_ff = np.zeros((node_count, node_count))
    burn_ff = np.zeros((node_count, node_count))
    cost_ff = np.zeros((node_count, node_count))
    for left_pos, left in enumerate(nodes):
        for right_pos, right in enumerate(nodes):
            minutes = physics.flight_minutes(aircraft_type, left, right)
            minutes_ff[left_pos, right_pos] = minutes
            burn_ff[left_pos, right_pos] = physics.fuel_for_leg(
                aircraft_type, left, right
            )
            cost_ff[left_pos, right_pos] = route_cost_multiplier * minutes
    minutes_start = np.empty(node_count)
    burn_start = np.empty(node_count)
    cost_start = np.empty(node_count)
    minutes_ret = np.empty(node_count)
    burn_ret = np.empty(node_count)
    cost_ret = np.empty(node_count)
    for pos, node in enumerate(nodes):
        minutes_start[pos] = physics.flight_minutes(aircraft_type, base_airport, node)
        burn_start[pos] = physics.fuel_for_leg(aircraft_type, base_airport, node)
        cost_start[pos] = route_cost_multiplier * minutes_start[pos]
        minutes_ret[pos] = physics.flight_minutes(aircraft_type, node, base_airport)
        burn_ret[pos] = physics.fuel_for_leg(aircraft_type, node, base_airport)
        cost_ret[pos] = route_cost_multiplier * minutes_ret[pos]
    for row, dual in branch_duals.items():
        traversal_cost = -float(dual) * row.canonical_sign
        left, right = row.arc
        if left == base_airport and right in index:
            cost_start[index[right]] += traversal_cost
        if right == base_airport and left in index:
            cost_ret[index[left]] += traversal_cost
        if left in index and right in index:
            cost_ff[index[left], index[right]] += traversal_cost
    return {
        "minutes_ff": minutes_ff,
        "burn_ff": burn_ff,
        "cost_ff": cost_ff,
        "minutes_start": minutes_start,
        "burn_start": burn_start,
        "cost_start": cost_start,
        "minutes_ret": minutes_ret,
        "burn_ret": burn_ret,
        "cost_ret": cost_ret,
    }


def _merge_signature(
    sig: tuple[float, ...],
    dest_units: tuple[float, ...],
    seats: int,
) -> tuple[float, ...]:
    """Merge a first-visited destination's units into the top-``seats`` tuple.

    Both inputs are sorted descending; the merge keeps the ``seats`` largest
    values. Lossless for the reward: a unit pushed out of the top ``seats``
    can never re-enter the top ``seats`` of any superset, and only positive
    top units ever contribute (or the forced single unit when none is
    positive), which the truncated tuple decides on its own.
    """

    merged: list[float] = []
    i = j = 0
    while len(merged) < seats and (i < len(sig) or j < len(dest_units)):
        if j >= len(dest_units) or (i < len(sig) and sig[i] >= dest_units[j]):
            merged.append(sig[i])
            i += 1
        else:
            merged.append(dest_units[j])
            j += 1
    return tuple(merged)


def _signature_reward(sig: tuple[float, ...]) -> float | None:
    """Optimal allocation reward for any visited set with this signature.

    Mirrors the MILP allocation polytope: choose between 1 and ``seats``
    units among visited destinations maximising dual value. The greedy
    top-``seats`` positive selection is optimal, with the forced single
    largest unit when nothing is positive. Empty signature = no allocation.
    """

    if not sig:
        return None
    positive = sum(value for value in sig if value > 0.0)
    if positive > 0.0:
        return positive
    return float(sig[0])


def _greedy_allocation(
    data: ProblemData,
    duals: Mapping[tuple[str, str], float],
    base_airport: str,
    seats: int,
    visited: frozenset[str],
) -> tuple[tuple[str, str, int], ...] | None:
    """Materialise the optimal allocation; exact mirror of the brute force."""

    units: list[tuple[float, tuple[str, str]]] = []
    for key, pool in sorted(data.q1_pools.items()):
        if key[1] not in visited or key[0] not in (base_airport, "LAND"):
            continue
        units.extend((float(duals.get(key, 0.0)), key) for _ in range(pool.quantity))
    units.sort(reverse=True)
    if not units:
        return None
    chosen = [unit for unit in units[:seats] if unit[0] > 0.0]
    if not chosen:
        chosen = units[:1]
    counts: dict[tuple[str, str], int] = {}
    for _, key in chosen:
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        (origin, destination, counts[(origin, destination)])
        for origin, destination in sorted(counts)
    )


def fast_exact_pricing(
    data: ProblemData,
    duals: Mapping[tuple[str, str], float],
    base_airport: str,
    aircraft_type: str,
    *,
    candidate_nodes: tuple[str, ...] | None = None,
    max_landings: int | None = None,
    branch_duals: Mapping[ArcBranchRow, float] | None = None,
    route_cost_multiplier: float = 1.0,
    time_limit_seconds: float | None = None,
    initial_incumbent_rc: float | None = None,
    return_diagnostics: bool = False,
) -> ExactPricingResult | tuple[ExactPricingResult, FastPricingDiagnostics]:
    """Exactly price one base/type subproblem via layered label-setting DP.

    Returns the same ``ExactPricingResult`` contract as the MILP oracle.
    ``proven_optimal=True`` certifies that no unenumerated route beats the
    reported reduced cost; ``certified_no_negative_column`` certifies that
    no negative reduced-cost column exists for this subproblem.

    ``initial_incumbent_rc`` may seed the pruning incumbent with the reduced
    cost of any known feasible column under these exact duals; it can only
    accelerate the search, never change the certified minimum.
    """

    started = time.perf_counter()
    if base_airport not in data.config.airports:
        raise ValueError(f"Unknown base airport: {base_airport}")
    if aircraft_type not in data.config.aircraft_types:
        raise ValueError(f"Unknown aircraft type: {aircraft_type}")
    nodes = tuple(sorted(candidate_nodes or data.config.facilities))
    if not nodes or any(node not in data.config.facilities for node in nodes):
        raise ValueError("Pricing candidate nodes must be sea facilities")
    landings = max_landings or data.config.max_sea_landings
    if not 1 <= landings <= data.config.max_sea_landings:
        raise ValueError("Pricing landing limit is outside the legal Q1 range")
    active_branch_duals = dict(branch_duals or {})
    if any(float(value) > 1.0e-7 for value in active_branch_duals.values()):
        raise ValueError("Canonical <= branch-row duals must be nonpositive")

    aircraft = data.config.aircraft_types[aircraft_type]
    seats = aircraft.seats
    tank = float(aircraft.tank_capacity_kg)
    reserve = float(aircraft.reserve_kg)
    physics = LegPhysics(data.config, data.matrix)
    arrays = _build_arc_arrays(
        data, base_airport, aircraft_type, nodes, physics,
        active_branch_duals, float(route_cost_multiplier),
    )
    minutes_ff = arrays["minutes_ff"]
    burn_ff = arrays["burn_ff"]
    cost_ff = arrays["cost_ff"]
    minutes_start = arrays["minutes_start"]
    burn_start = arrays["burn_start"]
    cost_start = arrays["cost_start"]
    minutes_ret = arrays["minutes_ret"]
    burn_ret = arrays["burn_ret"]
    cost_ret = arrays["cost_ret"]
    node_count = len(nodes)

    refuel_flags = np.asarray(
        [node in data.config.refuel_facilities for node in nodes]
    )
    stop_dwell = int(data.config.stop_without_refuel_minutes)
    refuel_extra = int(
        data.config.stop_with_refuel_minutes
        - data.config.stop_without_refuel_minutes
    )
    stop_cost = float(route_cost_multiplier) * stop_dwell
    refuel_cost = float(route_cost_multiplier) * refuel_extra

    # Per-destination truncated unit lists (only the top-`seats` units can
    # ever influence the reward) plus a valid global reward upper bound.
    unit_values_by_dest: dict[str, list[float]] = {}
    for key, pool in data.q1_pools.items():
        origin, destination = key
        if origin not in (base_airport, "LAND") or destination not in nodes:
            continue
        value = float(duals.get(key, 0.0))
        unit_values_by_dest.setdefault(destination, []).extend(
            [value] * pool.quantity
        )
    dest_units: dict[int, tuple[float, ...]] = {}
    for destination, values in unit_values_by_dest.items():
        dest_units[nodes.index(destination)] = tuple(
            sorted(values, reverse=True)[:seats]
        )
    global_sorted = sorted(
        (value for values in dest_units.values() for value in values),
        reverse=True,
    )[:seats]
    reward_max = float(sum(value for value in global_sorted if value > 0.0))
    # Per-destination positive top-seats sums in descending order with a
    # prefix-sum table: a label with K remaining first visits can add at
    # most the K largest of these (subadditivity of the top-seats positive
    # sum over destination multisets).
    dest_reward_prefix = [0.0]
    for value in sorted(
        (
            sum(unit for unit in units if unit > 0.0)
            for units in dest_units.values()
        ),
        reverse=True,
    ):
        dest_reward_prefix.append(dest_reward_prefix[-1] + value)
    leg_min = float(min(cost_ff.min(), cost_ret.min()))

    def _no_result(status_note: str) -> ExactPricingResult:
        return ExactPricingResult(
            base_airport=base_airport,
            aircraft_type=aircraft_type,
            status=status_note,
            proven_optimal=True,
            reduced_cost=None,
            route_duration_minutes=None,
            allocation_reward=None,
            allocation_pattern=(),
            stops=(),
            node_count=0,
            dual_bound=None,
            elapsed_seconds=round(time.perf_counter() - started, 6),
            candidate_nodes=node_count,
            max_landings=landings,
            repeated_visit=False,
            certified_no_negative_column=True,
            negative_column_found=False,
            route_cost_multiplier=float(route_cost_multiplier),
        )

    if not dest_units:
        # No eligible demand group can ever be served from this base within
        # the candidate universe: exactly the MILP's infeasible allocation.
        outcome = _no_result("NoFeasibleColumn")
        if return_diagnostics:
            return outcome, FastPricingDiagnostics(
                0, 0, 0, 0, 0, 0, 0, reward_max,
                round(time.perf_counter() - started, 6),
            )
        return outcome

    labels_created = 0
    dominance_prunes = 0
    bound_prunes = 0
    terminated_routes = 0
    incumbent_updates = 0
    labels_kept = 0
    incumbent_rc = float("inf")
    # Pruning incumbent: the seed is the reduced cost of any known feasible
    # column under these duals (>= true minimum), so it is safe for bounds
    # but never replaces the column we must materialize.
    prune_rc = (
        float(initial_incumbent_rc)
        if initial_incumbent_rc is not None else float("inf")
    )
    incumbent_duration = 0
    incumbent_pattern: tuple[tuple[str, str, int], ...] | None = None
    incumbent_visited: frozenset[str] = frozenset()
    incumbent_path: tuple[int, ...] = ()
    incumbent_flags: tuple[bool, ...] = ()

    def _consider_termination(
        node_index: int,
        cost: float,
        fuel: float,
        clock: int,
        sig: tuple[float, ...],
        path: tuple[int, ...],
        refuels: tuple[bool, ...],
    ) -> None:
        nonlocal incumbent_rc, incumbent_duration, incumbent_pattern
        nonlocal incumbent_visited, incumbent_path, incumbent_flags
        nonlocal terminated_routes, incumbent_updates, prune_rc
        if fuel + EPSILON < reserve + burn_ret[node_index]:
            return
        reward = _signature_reward(sig)
        if reward is None:
            return
        terminated_routes += 1
        rc = cost + cost_ret[node_index] - reward
        duration = clock + int(minutes_ret[node_index])
        if rc < incumbent_rc - _TIE_TOL:
            better = True
        elif (
            rc <= incumbent_rc + _TIE_TOL
            and incumbent_pattern is not None
        ):
            pattern = _greedy_allocation(
                data, duals, base_airport, seats,
                frozenset(nodes[i] for i in path),
            )
            better = (duration, pattern) < (incumbent_duration, incumbent_pattern)
        else:
            better = False
        if not better:
            return
        incumbent_updates += 1
        incumbent_rc = float(rc)
        prune_rc = min(prune_rc, incumbent_rc)
        incumbent_duration = duration
        incumbent_visited = frozenset(nodes[i] for i in path)
        incumbent_pattern = _greedy_allocation(
            data, duals, base_airport, seats, incumbent_visited
        )
        incumbent_path = path
        incumbent_flags = refuels

    def _pruned_by_bound(
        cost: float, sig: tuple[float, ...], remaining_landings: int
    ) -> bool:
        # Any kept label still needs at least one leg (intermediate or the
        # return leg) before completion, so the bound is valid. The reward
        # upper bound is the minimum of two valid bounds: the global
        # top-seats bound, and the label's own signature positive sum plus
        # the best `remaining_landings` per-destination rewards (a first
        # visit can only merge one destination's units, and the top-seats
        # positive sum is subadditive across destinations). The seed is the
        # reduced cost of a known feasible column and therefore >= the true
        # minimum, so no improving route can ever be pruned by it.
        sig_sum = sum(value for value in sig if value > 0.0)
        future = dest_reward_prefix[
            min(remaining_landings, len(dest_reward_prefix) - 1)
        ]
        reward_bound = min(reward_max, sig_sum + future)
        bound = cost + leg_min - reward_bound
        if incumbent_pattern is None:
            # Seed-only regime: nothing has been materialised yet, so
            # labels that could still tie the seed must survive to
            # termination; only labels provably worse than the seed are
            # removed.
            return bound > prune_rc + _TIE_TOL
        return bound >= prune_rc - _TIE_TOL

    def _check_time_limit() -> None:
        if (
            time_limit_seconds is not None
            and time.perf_counter() - started > time_limit_seconds
        ):
            raise RuntimeError(
                "Fast exact pricing hit its time limit; refusing to return "
                "an uncertified partial search."
            )

    # --- Layer 1: exactly one first landing from a full tank. -------------
    # Frontiers are keyed by (node, visited tuple of demand-bearing dests).
    raw_first: dict[tuple[int, tuple[int, ...]], list[tuple]] = {}
    arrival_first = tank - burn_start
    for node_index in np.flatnonzero(arrival_first + EPSILON >= reserve):
        node_index = int(node_index)
        units = dest_units.get(node_index)
        visited = (node_index,) if units is not None else ()
        sig = units if units is not None else ()
        path = (node_index,)
        raw = raw_first.setdefault((node_index, visited), [])
        clock0 = int(minutes_start[node_index])
        cost0 = float(cost_start[node_index]) + stop_cost
        arrival = float(arrival_first[node_index])
        labels_created += 1
        raw.append((cost0, arrival, clock0 + stop_dwell, sig, path, (False,)))
        if refuel_flags[node_index]:
            labels_created += 1
            raw.append((
                cost0 + refuel_cost, tank,
                clock0 + stop_dwell + refuel_extra, sig, path, (True,),
            ))
    frontier: dict[tuple[int, tuple[int, ...]], _LayerFrontier] = {}
    for key, raw in raw_first.items():
        front, pruned = _pareto_filter(raw)
        dominance_prunes += pruned
        node_index = key[0]
        for i in range(len(front)):
            _consider_termination(
                node_index, front.costs[i], front.fuels[i], front.times[i],
                front.sigs[i], front.paths[i], front.flags[i],
            )
        frontier[key] = front
    pruned_first: dict[tuple[int, tuple[int, ...]], _LayerFrontier] = {}
    for key, front in frontier.items():
        keep_indices: list[int] = []
        for i in range(len(front)):
            if _pruned_by_bound(front.costs[i], front.sigs[i], landings - 1):
                bound_prunes += 1
                continue
            keep_indices.append(i)
        if keep_indices:
            survivors = _LayerFrontier()
            survivors.costs = [front.costs[i] for i in keep_indices]
            survivors.fuels = [front.fuels[i] for i in keep_indices]
            survivors.times = [front.times[i] for i in keep_indices]
            survivors.sigs = [front.sigs[i] for i in keep_indices]
            survivors.paths = [front.paths[i] for i in keep_indices]
            survivors.flags = [front.flags[i] for i in keep_indices]
            frontier[key] = survivors
    labels_kept += sum(len(front) for front in frontier.values())
    _check_time_limit()

    # --- Layers 2..landings: extend, refuel-branch, terminate. ------------
    for _depth in range(2, landings + 1):
        next_raw: dict[tuple[int, tuple[int, ...]], list[tuple]] = {}
        for (node_index, visited), front in frontier.items():
            burn_row = burn_ff[node_index]
            cost_row = cost_ff[node_index]
            minutes_row = minutes_ff[node_index]
            for i in range(len(front)):
                fuel = front.fuels[i]
                arrival_row = fuel - burn_row
                targets = np.flatnonzero(arrival_row + EPSILON >= reserve)
                if not targets.size:
                    continue
                base_cost = front.costs[i] + stop_cost
                base_clock = front.times[i] + stop_dwell
                sig = front.sigs[i]
                path = front.paths[i]
                refuels = front.flags[i]
                for target in targets:
                    target = int(target)
                    cost = float(base_cost + cost_row[target])
                    arrival = float(arrival_row[target])
                    clock = int(base_clock + minutes_row[target])
                    units = dest_units.get(target)
                    if units is None or target in visited:
                        merged = sig
                        new_visited = visited
                    else:
                        merged = _merge_signature(sig, units, seats)
                        # Visited tuples stay sorted for canonical keys.
                        new_visited = tuple(sorted(visited + (target,)))
                    # Generation-time bound prune: the bound depends only on
                    # cost, signature and remaining landings, and a
                    # bound-pruned label can never terminate into the
                    # incumbent before its own layer's termination pass has
                    # run, so dropping it here is exactly the layer-end
                    # bound prune done earlier and cheaper.
                    if _pruned_by_bound(
                        cost, merged, landings - _depth
                    ):
                        bound_prunes += 1
                        continue
                    new_path = path + (target,)
                    raw = next_raw.setdefault((target, new_visited), [])
                    labels_created += 1
                    raw.append((
                        cost, arrival, clock, merged, new_path,
                        refuels + (False,),
                    ))
                    if refuel_flags[target]:
                        labels_created += 1
                        raw.append((
                            cost + refuel_cost, tank, clock + refuel_extra,
                            merged, new_path, refuels + (True,),
                        ))
        frontier = {}
        for (node_index, visited), raw in next_raw.items():
            front, pruned = _pareto_filter(raw)
            dominance_prunes += pruned
            keep_indices: list[int] = []
            for i in range(len(front)):
                _consider_termination(
                    node_index, front.costs[i], front.fuels[i],
                    front.times[i], front.sigs[i], front.paths[i],
                    front.flags[i],
                )
                if _pruned_by_bound(
                    front.costs[i], front.sigs[i], landings - _depth
                ):
                    bound_prunes += 1
                    continue
                keep_indices.append(i)
            if keep_indices:
                survivors = _LayerFrontier()
                survivors.costs = [front.costs[i] for i in keep_indices]
                survivors.fuels = [front.fuels[i] for i in keep_indices]
                survivors.times = [front.times[i] for i in keep_indices]
                survivors.sigs = [front.sigs[i] for i in keep_indices]
                survivors.paths = [front.paths[i] for i in keep_indices]
                survivors.flags = [front.flags[i] for i in keep_indices]
                frontier[(node_index, visited)] = survivors
        labels_kept += sum(len(front) for front in frontier.values())
        _check_time_limit()

    if incumbent_pattern is None:
        outcome = _no_result("NoFeasibleColumn")
        if return_diagnostics:
            return outcome, FastPricingDiagnostics(
                labels_created, labels_kept, dominance_prunes, bound_prunes,
                terminated_routes, incumbent_updates, landings, reward_max,
                round(time.perf_counter() - started, 6),
            )
        return outcome

    # --- Winner materialisation through the shared evaluator. -------------
    route_stops = [RouteStop(base_airport)]
    service_destinations = {
        destination for _, destination, _ in incumbent_pattern
    }
    first_service: set[str] = set()
    for node_index, refuel_flag in zip(incumbent_path, incumbent_flags):
        node = nodes[node_index]
        is_service = node in service_destinations and node not in first_service
        if is_service:
            first_service.add(node)
        route_stops.append(
            RouteStop(node, refuel=refuel_flag, is_service=is_service)
        )
    route_stops.append(RouteStop(base_airport))
    service_order: list[str] = []
    seen_service: set[str] = set()
    for node_index in incumbent_path:
        node = nodes[node_index]
        if node in service_destinations and node not in seen_service:
            seen_service.add(node)
            service_order.append(node)
    evaluation = evaluate_route(
        RoutePlan(
            base_airport, aircraft_type, tuple(route_stops), (),
            tuple(service_order),
        ),
        matrix=data.matrix,
        config=data.config,
    )
    if not evaluation.feasible:
        raise RuntimeError(
            f"Fast pricing produced illegal route: {evaluation.issues}"
        )
    if evaluation.total_aircraft_time_minutes != incumbent_duration:
        raise RuntimeError(
            "Fast pricing duration disagrees with shared evaluator: "
            f"internal={incumbent_duration}, "
            f"shared={evaluation.total_aircraft_time_minutes}"
        )
    priced_column = ExactRouteColumn(
        column_id="fast-pricing-verification",
        base_airport=base_airport,
        aircraft_type=aircraft_type,
        stops=tuple(route_stops),
        allocation_pattern=incumbent_pattern,
        duration_minutes=evaluation.total_aircraft_time_minutes,
        source="fast-pricing-verification",
    )
    reduced_cost = branch_column_reduced_cost(
        evaluation.total_aircraft_time_minutes,
        incumbent_pattern,
        duals,
        priced_column,
        active_branch_duals,
        route_cost_multiplier=route_cost_multiplier,
    )
    if abs(reduced_cost - incumbent_rc) > _CROSS_CHECK_TOL:
        raise RuntimeError(
            "Fast pricing internal accounting disagrees with shared "
            f"evaluator: internal={incumbent_rc}, shared={reduced_cost}"
        )
    elapsed = time.perf_counter() - started
    result = ExactPricingResult(
        base_airport=base_airport,
        aircraft_type=aircraft_type,
        status="Optimal",
        proven_optimal=True,
        reduced_cost=float(reduced_cost),
        route_duration_minutes=evaluation.total_aircraft_time_minutes,
        allocation_reward=float(sum(
            float(duals.get((origin, destination), 0.0)) * count
            for origin, destination, count in incumbent_pattern
        )),
        allocation_pattern=incumbent_pattern,
        stops=tuple(route_stops),
        node_count=labels_created,
        dual_bound=float(reduced_cost),
        elapsed_seconds=round(elapsed, 6),
        candidate_nodes=node_count,
        max_landings=landings,
        repeated_visit=len(incumbent_path) != len(set(incumbent_path)),
        certified_no_negative_column=bool(reduced_cost >= -PRICING_TOL),
        negative_column_found=bool(reduced_cost < -PRICING_TOL),
        branch_reduced_cost_contribution=float(
            -sum(
                float(dual) * row.canonical_coefficient(priced_column)
                for row, dual in active_branch_duals.items()
            )
        ),
        branch_coefficients=tuple(
            row.coefficient(priced_column) for row in active_branch_duals
        ),
        route_cost_multiplier=float(route_cost_multiplier),
    )
    if return_diagnostics:
        return result, FastPricingDiagnostics(
            labels_created, labels_kept, dominance_prunes, bound_prunes,
            terminated_routes, incumbent_updates, landings, reward_max,
            round(elapsed, 6),
        )
    return result
