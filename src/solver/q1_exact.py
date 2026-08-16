from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from scipy.sparse import vstack

from .data import ProblemData
from .models import PassengerAssignment, Solution
from .q1_or import (
    EliteRoutePool,
    Q1MasterConfig,
    _PatternArrays,
    _build_pattern_arrays,
    _materialize_pattern_master,
    _route_id,
    route_identity,
)


@dataclass(frozen=True)
class PatternMipStart:
    """A complete integer incumbent vector in allocated-pattern coordinates."""

    values: np.ndarray
    selected_columns: int
    selected_flights: int
    primary_objective: int
    passenger_objective: int
    flights_objective: int
    fuel_objective_kg: float
    maximum_equality_residual: float
    missing_patterns: tuple[str, ...]


@dataclass(frozen=True)
class HighsMasterResult:
    outcome: str
    model_status: str
    run_status: str
    solution: Solution | None
    solution_vector: np.ndarray | None
    objective: int | None
    dual_bound: float | None
    mip_gap: float | None
    node_count: int
    elapsed_seconds: float
    primary_upper_bound_minutes: int | None
    mip_start_backend_status: str | None
    mip_start_feasible_for_model: bool | None
    mip_start_maximum_row_violation: float | None
    stopped_for_stall: bool
    progress: tuple[dict[str, float], ...]


def canonical_allocation_pattern(
    assignments: Iterable[PassengerAssignment | tuple[str, str] | tuple[str, str, int]],
) -> tuple[tuple[str, str, int], ...]:
    """Canonical OD-count identity; passenger IDs and input order are irrelevant."""

    counts: Counter[tuple[str, str]] = Counter()
    for item in assignments:
        if isinstance(item, PassengerAssignment):
            origin, destination, count = item.origin_id, item.destination_id, 1
        elif len(item) == 2:
            origin, destination = item
            count = 1
        else:
            origin, destination, count = item
        count = int(count)
        if count <= 0:
            raise ValueError("Allocation pattern counts must be positive")
        counts[(str(origin), str(destination))] += count
    return tuple(
        (origin, destination, count)
        for (origin, destination), count in sorted(counts.items())
    )


def build_frozen_incumbent_start(
    data: ProblemData,
    pool: EliteRoutePool,
    solution: Solution,
    config: Q1MasterConfig | None = None,
) -> tuple[_PatternArrays, PatternMipStart]:
    """Map every frozen sortie to its exact physical-route/allocation column."""

    arrays = _build_pattern_arrays(data, pool, config or Q1MasterConfig())
    column_index = {
        (column.elite_route.route_id, column.pattern): index
        for index, column in enumerate(arrays.columns)
    }
    values = np.zeros(len(arrays.columns), dtype=float)
    missing: list[str] = []
    for route in solution.routes:
        route_id = _route_id(route_identity(route))
        pattern = canonical_allocation_pattern(route.assignments)
        index = column_index.get((route_id, pattern))
        if index is None:
            missing.append(f"{route_id}:{pattern}")
            continue
        values[index] += 1.0
    residual = arrays.equality @ values - arrays.equality_rhs
    maximum_residual = float(np.max(np.abs(residual))) if residual.size else 0.0
    return arrays, PatternMipStart(
        values=values,
        selected_columns=int(np.count_nonzero(values)),
        selected_flights=int(round(float(arrays.flights @ values))),
        primary_objective=int(round(float(arrays.primary @ values))),
        passenger_objective=int(round(float(arrays.passenger @ values))),
        flights_objective=int(round(float(arrays.flights @ values))),
        fuel_objective_kg=round(float(arrays.fuel_deci_kg @ values) / 10.0, 6),
        maximum_equality_residual=maximum_residual,
        missing_patterns=tuple(missing),
    )


def materialize_pattern_start(
    data: ProblemData,
    arrays: _PatternArrays,
    mip_start: PatternMipStart,
) -> Solution:
    if mip_start.missing_patterns:
        raise ValueError("Cannot materialize a MIP start with missing patterns")
    feasible, violation = _mip_start_feasibility(arrays, mip_start.values, None)
    if not feasible:
        raise ValueError(f"MIP start is not feasible: maximum violation={violation}")
    solution, _, _ = _materialize_pattern_master(data, arrays, mip_start.values)
    return solution


def audit_master_symmetry(
    data: ProblemData,
    pool: EliteRoutePool,
    config: Q1MasterConfig | None = None,
) -> dict[str, object]:
    """Audit only exact/canonical symmetry; do not merge distinct route semantics."""

    arrays = _build_pattern_arrays(data, pool, config or Q1MasterConfig())
    identities = [
        (column.elite_route.route_id, column.pattern) for column in arrays.columns
    ]
    identity_counts = Counter(identities)
    route_keys: dict[str, set[tuple[object, ...]]] = defaultdict(set)
    for elite in pool.routes:
        route_keys[elite.route_id].add(elite.key)

    coefficient_groups: dict[tuple[object, ...], list[int]] = defaultdict(list)
    for index, column in enumerate(arrays.columns):
        coverage = tuple(
            (origin, destination, count)
            for origin, destination, count in column.pattern
        )
        signature = (
            coverage,
            int(arrays.primary[index]),
            int(arrays.passenger[index]),
            int(arrays.flights[index]),
            int(arrays.fuel_deci_kg[index]),
            int(arrays.bounds.ub[index]),
        )
        coefficient_groups[signature].append(index)
    equivalent_groups = [group for group in coefficient_groups.values() if len(group) > 1]

    return {
        "representation": "aggregated OD-count allocated-route patterns",
        "individual_passenger_identity_variables": 0,
        "physical_routes": len(pool.routes),
        "allocated_pattern_columns": len(arrays.columns),
        "exact_duplicate_retained_columns": sum(
            count - 1 for count in identity_counts.values() if count > 1
        ),
        "route_id_semantic_collisions": sum(
            len(keys) > 1 for keys in route_keys.values()
        ),
        "coefficient_equivalent_groups_with_distinct_route_semantics": len(
            equivalent_groups
        ),
        "coefficient_equivalent_columns_with_distinct_route_semantics": sum(
            len(group) for group in equivalent_groups
        ),
        "safe_rules_applied": [
            "aggregate identical passenger IDs to exact OD counts",
            "sort and combine allocation counts into a canonical pattern",
            "hash full base/type/ordered physical-stop/refuel/service semantics",
            "deduplicate only identical (semantic route, canonical allocation) columns",
        ],
        "rules_explicitly_not_applied": [
            "do not merge distinct physical routes merely because master coefficients match",
            "do not prune routes by heuristic score, nearest-K, or source frequency",
        ],
    }


def _mip_start_feasibility(
    arrays: _PatternArrays,
    values: np.ndarray,
    primary_upper_bound_minutes: int | None,
) -> tuple[bool, float]:
    violations = [
        float(np.max(np.maximum(arrays.bounds.lb - values, 0.0))),
        float(np.max(np.maximum(values - arrays.bounds.ub, 0.0))),
        float(np.max(np.abs(values - np.rint(values)))),
        float(np.max(np.abs(arrays.equality @ values - arrays.equality_rhs))),
    ]
    if primary_upper_bound_minutes is not None:
        violations.append(
            max(
                0.0,
                float(arrays.primary @ values) - primary_upper_bound_minutes,
            )
        )
    maximum = max(violations)
    return maximum <= 1.0e-7, maximum


def solve_highs_pattern_master(
    data: ProblemData,
    pool: EliteRoutePool,
    *,
    config: Q1MasterConfig | None = None,
    mip_start: PatternMipStart | None = None,
    primary_upper_bound_minutes: int | None = None,
    time_limit_seconds: float | None = None,
    stall_limit_seconds: float | None = None,
    mip_max_nodes: int | None = None,
    random_seed: int = 0,
    log_path: Path | str | None = None,
    output_flag: bool = False,
) -> HighsMasterResult:
    """Solve the restricted integer master with direct HiGHS and a full MIP start."""

    import highspy

    config = config or Q1MasterConfig()
    arrays = _build_pattern_arrays(data, pool, config)
    matrix = arrays.equality
    row_lower = arrays.equality_rhs.copy()
    row_upper = arrays.equality_rhs.copy()
    if primary_upper_bound_minutes is not None:
        matrix = vstack([matrix, arrays.primary.reshape(1, -1)], format="csr")
        row_lower = np.concatenate([row_lower, [-highspy.kHighsInf]])
        row_upper = np.concatenate(
            [row_upper, [float(primary_upper_bound_minutes)]]
        )
    csc = matrix.tocsc()

    lp = highspy.HighsLp()
    lp.num_col_ = len(arrays.columns)
    lp.num_row_ = matrix.shape[0]
    lp.col_cost_ = arrays.primary
    lp.col_lower_ = arrays.bounds.lb
    lp.col_upper_ = arrays.bounds.ub
    lp.row_lower_ = row_lower
    lp.row_upper_ = row_upper
    lp.integrality_ = [highspy.HighsVarType.kInteger] * len(arrays.columns)
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.num_col_ = len(arrays.columns)
    lp.a_matrix_.num_row_ = matrix.shape[0]
    lp.a_matrix_.start_ = csc.indptr.astype(np.int32)
    lp.a_matrix_.index_ = csc.indices.astype(np.int32)
    lp.a_matrix_.value_ = csc.data.astype(float)

    highs = highspy.Highs()
    pass_status = highs.passModel(lp)
    if pass_status != highspy.HighsStatus.kOk:
        raise RuntimeError(f"HiGHS rejected master model: {pass_status}")
    highs.setOptionValue("output_flag", bool(output_flag))
    highs.setOptionValue("presolve", "on")
    highs.setOptionValue("mip_rel_gap", float(config.mip_relative_gap))
    highs.setOptionValue("random_seed", int(random_seed))
    if time_limit_seconds is not None:
        highs.setOptionValue("time_limit", float(time_limit_seconds))
    if mip_max_nodes is not None:
        highs.setOptionValue("mip_max_nodes", int(mip_max_nodes))
    if log_path is not None:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        highs.setOptionValue("log_file", str(log_path))

    start_status: str | None = None
    start_feasible: bool | None = None
    start_violation: float | None = None
    if mip_start is not None:
        if len(mip_start.values) != len(arrays.columns):
            raise ValueError("MIP start vector does not match master column mapping")
        start_feasible, start_violation = _mip_start_feasibility(
            arrays, mip_start.values, primary_upper_bound_minutes
        )
        status = highs.setSolution(
            len(arrays.columns),
            np.arange(len(arrays.columns), dtype=np.int32),
            mip_start.values.astype(float),
        )
        start_status = str(status)

    progress: list[dict[str, float]] = []
    stopped_for_stall = False
    last_signature: tuple[float, float, int] | None = None
    last_progress_time = 0.0

    def record_progress(event) -> None:
        nonlocal stopped_for_stall, last_signature, last_progress_time
        data_out = event.data_out
        signature = (
            float(data_out.mip_primal_bound),
            float(data_out.mip_dual_bound),
            int(data_out.mip_node_count),
        )
        running = float(data_out.running_time)
        if signature != last_signature:
            last_signature = signature
            last_progress_time = running
            progress.append(
                {
                    "running_time": running,
                    "primal_bound": signature[0],
                    "dual_bound": signature[1],
                    "node_count": float(signature[2]),
                    "gap": float(data_out.mip_gap),
                }
            )
        elif (
            stall_limit_seconds is not None
            and running - last_progress_time >= stall_limit_seconds
        ):
            stopped_for_stall = True
            event.interrupt()

    if stall_limit_seconds is not None:
        highs.cbMipLogging += record_progress

    started = time.perf_counter()
    run_status = highs.run()
    elapsed = time.perf_counter() - started
    model_status = highs.getModelStatus()
    info = highs.getInfo()
    highs_solution = highs.getSolution()
    vector: np.ndarray | None = None
    objective: int | None = None
    solution: Solution | None = None
    if (
        highs_solution.value_valid
        and info.primal_solution_status
        == highspy.SolutionStatus.kSolutionStatusFeasible
    ):
        vector = np.asarray(highs_solution.col_value, dtype=float)
        objective = int(round(float(arrays.primary @ vector)))
        solution, _, _ = _materialize_pattern_master(data, arrays, vector)

    if solution is not None and (
        primary_upper_bound_minutes is None
        or objective is not None
        and objective <= primary_upper_bound_minutes
    ):
        outcome = "FOUND_BETTER" if primary_upper_bound_minutes is not None else "FOUND"
    elif model_status == highspy.HighsModelStatus.kInfeasible:
        outcome = "PROVEN_RESTRICTED_INFEASIBLE"
    else:
        outcome = "UNKNOWN"

    dual_bound = float(info.mip_dual_bound)
    if not math.isfinite(dual_bound):
        dual_bound = None
    mip_gap = float(info.mip_gap)
    if not math.isfinite(mip_gap):
        mip_gap = None
    return HighsMasterResult(
        outcome=outcome,
        model_status=highs.modelStatusToString(model_status),
        run_status=str(run_status),
        solution=solution,
        solution_vector=vector,
        objective=objective,
        dual_bound=dual_bound,
        mip_gap=mip_gap,
        node_count=int(info.mip_node_count),
        elapsed_seconds=round(elapsed, 6),
        primary_upper_bound_minutes=primary_upper_bound_minutes,
        mip_start_backend_status=start_status,
        mip_start_feasible_for_model=start_feasible,
        mip_start_maximum_row_violation=start_violation,
        stopped_for_stall=stopped_for_stall,
        progress=tuple(progress),
    )
