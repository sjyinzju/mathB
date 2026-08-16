from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix, eye, hstack, vstack

from .data import ProblemData
from .evaluator import evaluate_route
from .models import PassengerAssignment, RoutePlan, Solution, aggregate_evaluations
from .q1_fast_pricing import fast_exact_pricing
from .q1_pricing import (
    PRICING_TOL,
    ArcBranchRow,
    ExactPricingResult,
    ExactRouteColumn,
    branch_column_reduced_cost,
    choose_fractional_arc_branch,
    exact_pricing,
    pricing_result_to_column,
)


LP_FEAS_TOL = 1.0e-8
INTEGER_TOL = 1.0e-7
CERT_TOL = 1.0e-6


@dataclass(frozen=True)
class ExactColumnMasterResult:
    outcome: str
    model_status: str
    run_status: str
    solution: Solution | None
    solution_vector: np.ndarray | None
    objective: int | None
    dual_bound: float | None
    mip_gap: float | None
    node_count: int
    lp_iterations: int
    elapsed_seconds: float
    primary_upper_bound_minutes: int | None
    mip_start_backend_status: str | None
    mip_start_feasible_for_model: bool | None
    mip_start_maximum_row_violation: float | None


@dataclass(frozen=True)
class BranchRmpLPResult:
    status: str
    proven_optimal: bool
    proven_infeasible: bool
    objective: float | None
    demand_duals: dict[tuple[str, str], float]
    branch_duals: dict[ArcBranchRow, float]
    reduced_costs: np.ndarray | None
    selected_values: np.ndarray | None
    elapsed_seconds: float


@dataclass(frozen=True)
class BranchNode:
    node_id: str
    parent_id: str | None
    depth: int
    branch_history: tuple[ArcBranchRow, ...]
    inherited_lb: float


@dataclass(frozen=True)
class FullyPricedIteration:
    iteration: int
    phase: str
    rmp: BranchRmpLPResult
    pricing_results: tuple[ExactPricingResult, ...]
    added_column_ids: tuple[str, ...]
    elapsed_seconds: float


@dataclass(frozen=True)
class FullyPricedNodeResult:
    node: BranchNode
    status: str
    initial_rmp_objective: float | None
    fully_priced_lb: float | None
    effective_rigorous_lb: float
    columns_before: int
    columns_after: int
    generated_columns: tuple[ExactRouteColumn, ...]
    iterations: tuple[FullyPricedIteration, ...]
    pricing_calls: int
    fractional_route_variables: int | None
    selected_values: np.ndarray | None
    selected_branch: tuple[tuple[str, str], float] | None
    rmp_seconds: float
    pricing_seconds: float
    elapsed_seconds: float


def load_generated_exact_columns(path: Path | str) -> tuple[ExactRouteColumn, ...]:
    """Restore stable generated columns without serializing solver internals."""

    from .models import RouteStop

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_columns = payload.get("generated_columns", payload.get("new_columns"))
    if raw_columns is None:
        raise ValueError("Exact-column checkpoint has no serialized column list")
    return tuple(
        ExactRouteColumn(
            column_id=raw["column_id"],
            base_airport=raw["base_airport"],
            aircraft_type=raw["aircraft_type"],
            stops=tuple(RouteStop(**stop) for stop in raw["stops"]),
            allocation_pattern=tuple(tuple(item) for item in raw["allocation_pattern"]),
            duration_minutes=int(raw["duration_minutes"]),
            source=raw["source"],
        )
        for raw in raw_columns
    )


def exact_column_demand_matrix(
    data: ProblemData,
    columns: Iterable[ExactRouteColumn],
):
    columns = tuple(columns)
    group_keys = tuple(sorted(data.q1_pools))
    group_index = {key: index for index, key in enumerate(group_keys)}
    rows: list[int] = []
    column_indices: list[int] = []
    values: list[float] = []
    for column_index, column in enumerate(columns):
        for origin, destination, count in column.allocation_pattern:
            key = (origin, destination)
            if key not in group_index:
                raise ValueError(f"Column covers unknown demand group: {key}")
            rows.append(group_index[key])
            column_indices.append(column_index)
            values.append(float(count))
    matrix = coo_matrix(
        (values, (rows, column_indices)),
        shape=(len(group_keys), len(columns)),
    ).tocsr()
    rhs = np.asarray([data.q1_pools[key].quantity for key in group_keys], dtype=float)
    costs = np.asarray([column.duration_minutes for column in columns], dtype=float)
    return columns, group_keys, matrix, rhs, costs


def solve_branch_rmp_lp(
    data: ProblemData,
    columns: Iterable[ExactRouteColumn],
    branch_history: Iterable[ArcBranchRow] = (),
) -> BranchRmpLPResult:
    """Solve one node RMP using canonical branch rows s*b*x <= s*k."""

    columns, group_keys, equality, rhs, costs = exact_column_demand_matrix(
        data, columns
    )
    branch_history = tuple(branch_history)
    ub_matrix = None
    ub_rhs = None
    if branch_history:
        rows: list[int] = []
        column_indices: list[int] = []
        values: list[float] = []
        for row_index, branch_row in enumerate(branch_history):
            for column_index, column in enumerate(columns):
                value = branch_row.canonical_coefficient(column)
                if value:
                    rows.append(row_index)
                    column_indices.append(column_index)
                    values.append(value)
        ub_matrix = coo_matrix(
            (values, (rows, column_indices)),
            shape=(len(branch_history), len(columns)),
        ).tocsr()
        ub_rhs = np.asarray(
            [row.canonical_rhs for row in branch_history], dtype=float
        )
    started = time.perf_counter()
    result = linprog(
        costs,
        A_ub=ub_matrix,
        b_ub=ub_rhs,
        A_eq=equality,
        b_eq=rhs,
        bounds=(0.0, None),
        method="highs",
        options={
            "primal_feasibility_tolerance": LP_FEAS_TOL,
            "dual_feasibility_tolerance": LP_FEAS_TOL,
        },
    )
    elapsed = round(time.perf_counter() - started, 6)
    if result.status == 2:
        return BranchRmpLPResult(
            status=str(result.message),
            proven_optimal=False,
            proven_infeasible=True,
            objective=None,
            demand_duals={},
            branch_duals={},
            reduced_costs=None,
            selected_values=None,
            elapsed_seconds=elapsed,
        )
    if not result.success or result.x is None:
        return BranchRmpLPResult(
            status=str(result.message),
            proven_optimal=False,
            proven_infeasible=False,
            objective=None,
            demand_duals={},
            branch_duals={},
            reduced_costs=None,
            selected_values=None,
            elapsed_seconds=elapsed,
        )
    demand_vector = np.asarray(result.eqlin.marginals, dtype=float)
    branch_vector = (
        np.asarray(result.ineqlin.marginals, dtype=float)
        if branch_history else np.asarray([], dtype=float)
    )
    if branch_vector.size and float(branch_vector.max()) > LP_FEAS_TOL:
        raise RuntimeError("Canonical <= branch dual has invalid positive sign")
    reduced = costs - equality.T @ demand_vector
    if branch_history and ub_matrix is not None:
        reduced = reduced - ub_matrix.T @ branch_vector
    return BranchRmpLPResult(
        status=str(result.message),
        proven_optimal=True,
        proven_infeasible=False,
        objective=float(result.fun),
        demand_duals={
            key: float(demand_vector[index])
            for index, key in enumerate(group_keys)
        },
        branch_duals={
            row: float(branch_vector[index])
            for index, row in enumerate(branch_history)
        },
        reduced_costs=np.asarray(reduced, dtype=float),
        selected_values=np.asarray(result.x, dtype=float),
        elapsed_seconds=elapsed,
    )


def _branch_matrix(columns, branch_history):
    rows: list[int] = []
    column_indices: list[int] = []
    values: list[float] = []
    for row_index, branch_row in enumerate(branch_history):
        for column_index, column in enumerate(columns):
            value = branch_row.canonical_coefficient(column)
            if value:
                rows.append(row_index)
                column_indices.append(column_index)
                values.append(value)
    return coo_matrix(
        (values, (rows, column_indices)),
        shape=(len(branch_history), len(columns)),
    ).tocsr()


def solve_branch_phase_one_rmp_lp(
    data: ProblemData,
    columns: Iterable[ExactRouteColumn],
    branch_history: Iterable[ArcBranchRow] = (),
) -> BranchRmpLPResult:
    """Elastic Phase-I RMP used to obtain a rigorous Farkas pricing gate."""

    columns, group_keys, equality, rhs, _ = exact_column_demand_matrix(data, columns)
    branch_history = tuple(branch_history)
    demand_count = len(group_keys)
    branch_count = len(branch_history)
    column_count = len(columns)
    # A*x + p - n = d and alpha*x - v <= beta.  The objective is the
    # unweighted sum of all nonnegative violations.
    equality_augmented = hstack(
        [
            equality,
            eye(demand_count, format="csr"),
            -eye(demand_count, format="csr"),
            coo_matrix((demand_count, branch_count)).tocsr(),
        ],
        format="csr",
    )
    ub_augmented = None
    ub_rhs = None
    branch_matrix = None
    if branch_history:
        branch_matrix = _branch_matrix(columns, branch_history)
        ub_augmented = hstack(
            [
                branch_matrix,
                coo_matrix((branch_count, 2 * demand_count)).tocsr(),
                -eye(branch_count, format="csr"),
            ],
            format="csr",
        )
        ub_rhs = np.asarray(
            [row.canonical_rhs for row in branch_history], dtype=float
        )
    costs = np.concatenate(
        [
            np.zeros(column_count),
            np.ones(2 * demand_count + branch_count),
        ]
    )
    started = time.perf_counter()
    result = linprog(
        costs,
        A_ub=ub_augmented,
        b_ub=ub_rhs,
        A_eq=equality_augmented,
        b_eq=rhs,
        bounds=(0.0, None),
        method="highs",
        options={
            "primal_feasibility_tolerance": LP_FEAS_TOL,
            "dual_feasibility_tolerance": LP_FEAS_TOL,
        },
    )
    elapsed = round(time.perf_counter() - started, 6)
    if not result.success or result.x is None:
        return BranchRmpLPResult(
            status=str(result.message),
            proven_optimal=False,
            proven_infeasible=result.status == 2,
            objective=None,
            demand_duals={},
            branch_duals={},
            reduced_costs=None,
            selected_values=None,
            elapsed_seconds=elapsed,
        )
    demand_vector = np.asarray(result.eqlin.marginals, dtype=float)
    branch_vector = (
        np.asarray(result.ineqlin.marginals, dtype=float)
        if branch_history else np.asarray([], dtype=float)
    )
    reduced = -equality.T @ demand_vector
    if branch_history and branch_matrix is not None:
        reduced = reduced - branch_matrix.T @ branch_vector
    return BranchRmpLPResult(
        status=str(result.message),
        proven_optimal=True,
        proven_infeasible=False,
        objective=float(result.fun),
        demand_duals={
            key: float(demand_vector[index])
            for index, key in enumerate(group_keys)
        },
        branch_duals={
            row: float(branch_vector[index])
            for index, row in enumerate(branch_history)
        },
        reduced_costs=np.asarray(reduced, dtype=float),
        selected_values=np.asarray(result.x[:column_count], dtype=float),
        elapsed_seconds=elapsed,
    )


def solve_fully_priced_node(
    data: ProblemData,
    registry_columns: Iterable[ExactRouteColumn],
    node: BranchNode,
    *,
    workers: int = 3,
    pricing_time_limit_seconds: float | None = None,
    max_iterations: int | None = None,
) -> FullyPricedNodeResult:
    """Run true node RMP -> nine exact pricing problems to the final gate."""

    started = time.perf_counter()
    columns = list(registry_columns)
    identities = {column.identity for column in columns}
    generated: list[ExactRouteColumn] = []
    iteration_records: list[FullyPricedIteration] = []
    initial_objective = None
    final_rmp = None
    rmp_seconds = 0.0
    pricing_seconds = 0.0
    status = "UNPRICED_INCOMPLETE"
    iteration = 0
    phase = "PHASE_II"
    while True:
        if max_iterations is not None and iteration >= max_iterations:
            status = "UNPRICED_ITERATION_LIMIT"
            break
        iteration_started = time.perf_counter()
        rmp = (
            solve_branch_phase_one_rmp_lp(data, columns, node.branch_history)
            if phase == "PHASE_I"
            else solve_branch_rmp_lp(data, columns, node.branch_history)
        )
        if phase == "PHASE_II" and rmp.proven_infeasible:
            phase = "PHASE_I"
            rmp = solve_branch_phase_one_rmp_lp(
                data, columns, node.branch_history
            )
        rmp_seconds += rmp.elapsed_seconds
        if initial_objective is None:
            initial_objective = rmp.objective
        if not rmp.proven_optimal:
            status = "UNPRICED_PHASE_ONE_FAILURE" if phase == "PHASE_I" else "UNPRICED_RMP_FAILURE"
            final_rmp = rmp
            break

        tasks = [
            (base, aircraft_type)
            for base in data.config.airports
            for aircraft_type in data.config.aircraft_types
        ]
        multiplier = 0.0 if phase == "PHASE_I" else 1.0
        # Seed the fast oracle's pruning incumbent per subproblem with the
        # best registry column reduced cost under the current duals. Every
        # registry column satisfies this node's branch rows, so its reduced
        # cost upper-bounds the subproblem minimum and seeding can never
        # remove an improving column.
        seeds: dict[tuple[str, str], float] = {}
        for column in columns:
            key = (column.base_airport, column.aircraft_type)
            rc = branch_column_reduced_cost(
                column.duration_minutes,
                column.allocation_pattern,
                rmp.demand_duals,
                column,
                rmp.branch_duals,
                route_cost_multiplier=multiplier,
            )
            if key not in seeds or rc < seeds[key]:
                seeds[key] = rc

        def _price_one(base: str, aircraft_type: str) -> ExactPricingResult:
            try:
                return fast_exact_pricing(
                    data,
                    rmp.demand_duals,
                    base,
                    aircraft_type,
                    branch_duals=rmp.branch_duals,
                    route_cost_multiplier=multiplier,
                    time_limit_seconds=pricing_time_limit_seconds,
                    initial_incumbent_rc=seeds.get((base, aircraft_type)),
                )
            except Exception as failure:  # noqa: BLE001
                # The HiGHS MILP oracle remains the permanent reference and
                # fallback: any fast-oracle internal disagreement, time
                # limit, or unexpected state falls back to it.
                print(
                    f"[q1_branch_price] fast pricing fallback for "
                    f"{base}-{aircraft_type}: {failure}"
                )
                return exact_pricing(
                    data,
                    rmp.demand_duals,
                    base,
                    aircraft_type,
                    branch_duals=rmp.branch_duals,
                    route_cost_multiplier=multiplier,
                    time_limit_seconds=pricing_time_limit_seconds,
                )

        pricing_started = time.perf_counter()
        pricing_results: list[ExactPricingResult] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(_price_one, base, aircraft_type): (
                    base, aircraft_type
                )
                for base, aircraft_type in tasks
            }
            for future in as_completed(future_map):
                pricing_results.append(future.result())
        pricing_results.sort(
            key=lambda result: (result.base_airport, result.aircraft_type)
        )
        pricing_seconds += time.perf_counter() - pricing_started
        exact_gate = all(result.proven_optimal for result in pricing_results)
        negative = [
            result for result in pricing_results
            if result.reduced_cost is not None
            and result.reduced_cost < -PRICING_TOL
        ]
        added: list[ExactRouteColumn] = []
        for result in negative:
            column = pricing_result_to_column(
                result, source=f"branch_price_{node.node_id}_iteration_{iteration}"
            )
            if column.identity in identities:
                continue
            identities.add(column.identity)
            columns.append(column)
            generated.append(column)
            added.append(column)
        iteration_records.append(
            FullyPricedIteration(
                iteration=iteration,
                phase=phase,
                rmp=rmp,
                pricing_results=tuple(pricing_results),
                added_column_ids=tuple(column.column_id for column in added),
                elapsed_seconds=round(
                    time.perf_counter() - iteration_started, 6
                ),
            )
        )
        final_rmp = rmp
        if not exact_gate:
            status = "EXACT_PRICING_FAILURE"
            break
        if negative and not added:
            status = "DUPLICATE_NEGATIVE_COLUMN_ERROR"
            break
        if not added:
            if all(
                result.certified_no_negative_column
                for result in pricing_results
            ):
                if phase == "PHASE_I":
                    if rmp.objective is None:
                        status = "UNPRICED_PHASE_ONE_FAILURE"
                    elif rmp.objective > LP_FEAS_TOL:
                        status = "FULLY_PRICED_INFEASIBLE"
                        break
                    else:
                        phase = "PHASE_II"
                        iteration += 1
                        continue
                else:
                    status = "FULLY_PRICED"
            else:
                status = "EXACT_PRICING_FAILURE"
            break
        iteration += 1

    selected = (
        final_rmp.selected_values
        if status == "FULLY_PRICED" and final_rmp is not None else None
    )
    fractional = (
        int(sum(abs(value - round(value)) > INTEGER_TOL for value in selected))
        if selected is not None else None
    )
    branch = (
        choose_fractional_arc_branch(columns, selected, tolerance=INTEGER_TOL)
        if selected is not None and fractional else None
    )
    fully_priced_lb = (
        final_rmp.objective
        if status == "FULLY_PRICED" and final_rmp is not None else None
    )
    return FullyPricedNodeResult(
        node=node,
        status=status,
        initial_rmp_objective=initial_objective,
        fully_priced_lb=fully_priced_lb,
        effective_rigorous_lb=(
            math.inf
            if status == "FULLY_PRICED_INFEASIBLE"
            else float(fully_priced_lb)
            if fully_priced_lb is not None
            else float(node.inherited_lb)
        ),
        columns_before=len(columns) - len(generated),
        columns_after=len(columns),
        generated_columns=tuple(generated),
        iterations=tuple(iteration_records),
        pricing_calls=sum(len(record.pricing_results) for record in iteration_records),
        fractional_route_variables=fractional,
        selected_values=selected,
        selected_branch=branch,
        rmp_seconds=round(rmp_seconds, 6),
        pricing_seconds=round(pricing_seconds, 6),
        elapsed_seconds=round(time.perf_counter() - started, 6),
    )


def materialize_exact_column_solution(
    data: ProblemData,
    columns: Iterable[ExactRouteColumn],
    selected: np.ndarray,
) -> Solution:
    columns = tuple(columns)
    values = np.asarray(selected, dtype=float)
    if len(values) != len(columns):
        raise ValueError("Selected vector does not match exact columns")
    if np.max(np.abs(values - np.rint(values))) > INTEGER_TOL:
        raise ValueError("Cannot materialize a fractional exact-column solution")
    remaining = {key: list(pool.person_ids) for key, pool in data.q1_pools.items()}
    routes: list[RoutePlan] = []
    evaluations = []
    for column, multiplicity in zip(columns, np.rint(values).astype(int)):
        if multiplicity <= 0:
            continue
        locations = tuple(stop.facility_id for stop in column.stops)
        service_order: list[str] = []
        for _, destination, _ in column.allocation_pattern:
            if destination not in service_order:
                service_order.append(destination)
        for _ in range(int(multiplicity)):
            assignments: list[PassengerAssignment] = []
            for origin, destination, count in column.allocation_pattern:
                key = (origin, destination)
                people = remaining[key][:count]
                del remaining[key][:count]
                if len(people) != count:
                    raise RuntimeError("Exact-column allocation exceeds remaining demand")
                delivery = locations.index(destination, 1)
                assignments.extend(
                    PassengerAssignment(person, origin, destination, 0, delivery)
                    for person in people
                )
            route = RoutePlan(
                base_airport=column.base_airport,
                aircraft_type=column.aircraft_type,
                stops=column.stops,
                assignments=tuple(sorted(assignments, key=lambda item: item.person_id)),
                service_facilities=tuple(service_order),
            )
            evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
            if not evaluation.feasible:
                raise RuntimeError(f"Exact-column route invalid: {evaluation.issues}")
            routes.append(route)
            evaluations.append(evaluation)
    if any(remaining.values()):
        raise RuntimeError("Exact-column master did not cover all demand")
    return Solution(
        routes=tuple(routes),
        metrics=aggregate_evaluations(evaluations, data.q1_passenger_count),
        method="q1_exact_column_integer_master",
        diagnostics={
            "column_count": len(columns),
            "selected_columns": int(np.count_nonzero(np.rint(values))),
        },
    )


def exact_column_start_from_pattern_start(
    pattern_values: np.ndarray,
    exact_column_count: int,
) -> np.ndarray:
    """The first exact columns retain the deterministic pattern-array ordering."""

    values = np.asarray(pattern_values, dtype=float)
    if len(values) > exact_column_count:
        raise ValueError("Pattern start has more entries than exact-column registry")
    start = np.zeros(exact_column_count, dtype=float)
    start[: len(values)] = values
    return start


def _start_feasibility(
    matrix,
    rhs: np.ndarray,
    costs: np.ndarray,
    values: np.ndarray,
    primary_upper_bound_minutes: int | None,
    branch_matrix=None,
    branch_rhs: np.ndarray | None = None,
) -> tuple[bool, float]:
    violations = [
        float(np.max(np.maximum(-values, 0.0))),
        float(np.max(np.abs(values - np.rint(values)))),
        float(np.max(np.abs(matrix @ values - rhs))),
    ]
    if primary_upper_bound_minutes is not None:
        violations.append(
            max(0.0, float(costs @ values) - primary_upper_bound_minutes)
        )
    if branch_matrix is not None and branch_rhs is not None:
        violations.append(
            float(np.max(np.maximum(branch_matrix @ values - branch_rhs, 0.0)))
        )
    maximum = max(violations)
    return maximum <= INTEGER_TOL, maximum


def solve_exact_column_integer_master(
    data: ProblemData,
    columns: Iterable[ExactRouteColumn],
    *,
    mip_start_values: np.ndarray | None = None,
    branch_history: Iterable[ArcBranchRow] = (),
    primary_upper_bound_minutes: int | None = None,
    time_limit_seconds: float | None = None,
    random_seed: int = 0,
    log_path: Path | str | None = None,
    output_flag: bool = False,
    submit_infeasible_mip_start: bool = False,
) -> ExactColumnMasterResult:
    """Solve the integer master over an explicit exact-column registry."""

    import highspy

    columns, _, demand_matrix, rhs, costs = exact_column_demand_matrix(data, columns)
    matrix = demand_matrix
    row_lower = rhs.copy()
    row_upper = rhs.copy()
    branch_history = tuple(branch_history)
    branch_matrix = None
    branch_rhs = None
    if branch_history:
        branch_rows: list[int] = []
        branch_columns: list[int] = []
        branch_values: list[float] = []
        for row_index, branch_row in enumerate(branch_history):
            for column_index, column in enumerate(columns):
                value = branch_row.canonical_coefficient(column)
                if value:
                    branch_rows.append(row_index)
                    branch_columns.append(column_index)
                    branch_values.append(value)
        branch_matrix = coo_matrix(
            (branch_values, (branch_rows, branch_columns)),
            shape=(len(branch_history), len(columns)),
        ).tocsr()
        branch_rhs = np.asarray(
            [row.canonical_rhs for row in branch_history], dtype=float
        )
        matrix = vstack([matrix, branch_matrix], format="csr")
        row_lower = np.concatenate(
            [row_lower, np.full(len(branch_history), -highspy.kHighsInf)]
        )
        row_upper = np.concatenate([row_upper, branch_rhs])
    if primary_upper_bound_minutes is not None:
        matrix = vstack([matrix, costs.reshape(1, -1)], format="csr")
        row_lower = np.concatenate([row_lower, [-highspy.kHighsInf]])
        row_upper = np.concatenate([row_upper, [float(primary_upper_bound_minutes)]])
    csc = matrix.tocsc()
    lp = highspy.HighsLp()
    lp.num_col_ = len(columns)
    lp.num_row_ = matrix.shape[0]
    lp.col_cost_ = costs
    lp.col_lower_ = np.zeros(len(columns))
    # Every column covers at least one passenger.  Demand equalities therefore
    # imply this finite integer-safe multiplicity bound.  Supplying it explicitly
    # avoids unstable presolve transformations without tightening the integer set.
    demand_by_key = {key: int(data.q1_pools[key].quantity) for key in data.q1_pools}
    lp.col_upper_ = np.asarray(
        [
            min(
                demand_by_key[(origin, destination)] // int(count)
                for origin, destination, count in column.allocation_pattern
            )
            for column in columns
        ],
        dtype=float,
    )
    lp.row_lower_ = row_lower
    lp.row_upper_ = row_upper
    lp.integrality_ = [highspy.HighsVarType.kInteger] * len(columns)
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.num_col_ = lp.num_col_
    lp.a_matrix_.num_row_ = lp.num_row_
    lp.a_matrix_.start_ = csc.indptr.astype(np.int32)
    lp.a_matrix_.index_ = csc.indices.astype(np.int32)
    lp.a_matrix_.value_ = csc.data.astype(float)

    highs = highspy.Highs()
    if highs.passModel(lp) != highspy.HighsStatus.kOk:
        raise RuntimeError("HiGHS rejected exact-column integer master")
    highs.setOptionValue("output_flag", bool(output_flag))
    highs.setOptionValue("presolve", "on")
    highs.setOptionValue("mip_rel_gap", 0.0)
    highs.setOptionValue("mip_abs_gap", 1.0e-9)
    highs.setOptionValue("mip_feasibility_tolerance", LP_FEAS_TOL)
    highs.setOptionValue("primal_feasibility_tolerance", LP_FEAS_TOL)
    highs.setOptionValue("dual_feasibility_tolerance", LP_FEAS_TOL)
    # The Master already has exact canonical OD-count columns.  HiGHS symmetry
    # transformations produced rejected untransformed candidates with demand-row
    # residual 8 on this instance, so certification mode disables that backend
    # transformation and searches the original integer model directly.
    highs.setOptionValue("mip_detect_symmetry", False)
    highs.setOptionValue("random_seed", int(random_seed))
    if time_limit_seconds is not None:
        highs.setOptionValue("time_limit", float(time_limit_seconds))
    if log_path is not None:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        highs.setOptionValue("log_file", str(log_path))
        highs.setOptionValue("log_to_console", False)

    start_status = None
    start_feasible = None
    start_violation = None
    if mip_start_values is not None:
        start_values = np.asarray(mip_start_values, dtype=float)
        if len(start_values) != len(columns):
            raise ValueError("MIP start vector does not match exact-column registry")
        start_feasible, start_violation = _start_feasibility(
            demand_matrix,
            rhs,
            costs,
            start_values,
            primary_upper_bound_minutes,
            branch_matrix,
            branch_rhs,
        )
        if start_feasible or submit_infeasible_mip_start:
            start_status = str(
                highs.setSolution(
                    len(columns),
                    np.arange(len(columns), dtype=np.int32),
                    start_values,
                )
            )

    started = time.perf_counter()
    run_status = highs.run()
    elapsed = time.perf_counter() - started
    status = highs.getModelStatus()
    info = highs.getInfo()
    backend_solution = highs.getSolution()
    vector = None
    objective = None
    solution = None
    if (
        backend_solution.value_valid
        and info.primal_solution_status
        == highspy.SolutionStatus.kSolutionStatusFeasible
    ):
        vector = np.asarray(backend_solution.col_value, dtype=float)
        objective = int(round(float(costs @ vector)))
        solution = materialize_exact_column_solution(data, columns, vector)

    if status == highspy.HighsModelStatus.kOptimal and solution is not None:
        outcome = "PROVEN_POOL_INTEGER_OPTIMAL"
    elif status == highspy.HighsModelStatus.kInfeasible:
        outcome = "PROVEN_POOL_INFEASIBLE"
    elif solution is not None:
        outcome = "FEASIBLE_NOT_PROVEN"
    else:
        outcome = "UNKNOWN"
    dual_bound = float(info.mip_dual_bound)
    if not math.isfinite(dual_bound):
        dual_bound = None
    mip_gap = float(info.mip_gap)
    if not math.isfinite(mip_gap):
        mip_gap = None
    return ExactColumnMasterResult(
        outcome=outcome,
        model_status=highs.modelStatusToString(status),
        run_status=str(run_status),
        solution=solution,
        solution_vector=vector,
        objective=objective,
        dual_bound=dual_bound,
        mip_gap=mip_gap,
        node_count=int(info.mip_node_count),
        lp_iterations=int(info.simplex_iteration_count),
        elapsed_seconds=round(elapsed, 6),
        primary_upper_bound_minutes=primary_upper_bound_minutes,
        mip_start_backend_status=start_status,
        mip_start_feasible_for_model=start_feasible,
        mip_start_maximum_row_violation=start_violation,
    )
