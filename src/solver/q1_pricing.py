from __future__ import annotations

import math
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from itertools import product
from typing import Iterable, Mapping

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from .data import ProblemData
from .evaluator import evaluate_route
from .models import PassengerAssignment, RoutePlan, RouteStop
from .physics import LegPhysics
from .q1_or import EliteRoutePool, Q1MasterConfig, _build_pattern_arrays


PRICING_TOL = 1.0e-7


@dataclass(frozen=True)
class FullspaceRmpLPResult:
    objective: float
    demand_duals: dict[tuple[str, str], float]
    reduced_costs: np.ndarray
    selected_values: np.ndarray
    elapsed_seconds: float


@dataclass(frozen=True)
class ExactPricingResult:
    base_airport: str
    aircraft_type: str
    status: str
    proven_optimal: bool
    reduced_cost: float | None
    route_duration_minutes: int | None
    allocation_reward: float | None
    allocation_pattern: tuple[tuple[str, str, int], ...]
    stops: tuple[RouteStop, ...]
    node_count: int
    dual_bound: float | None
    elapsed_seconds: float
    candidate_nodes: int
    max_landings: int
    repeated_visit: bool
    certified_no_negative_column: bool
    negative_column_found: bool
    branch_reduced_cost_contribution: float = 0.0
    branch_coefficients: tuple[int, ...] = ()
    route_cost_multiplier: float = 1.0


@dataclass(frozen=True)
class ExactRouteColumn:
    column_id: str
    base_airport: str
    aircraft_type: str
    stops: tuple[RouteStop, ...]
    allocation_pattern: tuple[tuple[str, str, int], ...]
    duration_minutes: int
    source: str

    @property
    def identity(self) -> tuple[object, ...]:
        return (
            self.base_airport,
            self.aircraft_type,
            tuple(
                (stop.facility_id, bool(stop.refuel), bool(stop.is_service))
                for stop in self.stops
            ),
            self.allocation_pattern,
        )


@dataclass(frozen=True)
class ArcBranchRow:
    """Integer directed-arc usage disjunction row at a B&P node."""

    arc: tuple[str, str]
    sense: str
    rhs: int

    def __post_init__(self) -> None:
        if len(self.arc) != 2:
            raise ValueError("A branch arc must contain two locations")
        if self.sense not in ("<=", ">="):
            raise ValueError("Branch sense must be <= or >=")

    @property
    def canonical_sign(self) -> float:
        """Multiplier giving canonical row sign*b*x <= sign*rhs."""

        return 1.0 if self.sense == "<=" else -1.0

    @property
    def canonical_rhs(self) -> float:
        return self.canonical_sign * float(self.rhs)

    def coefficient(self, column: ExactRouteColumn) -> int:
        return column_arc_counts(column).get(self.arc, 0)

    def canonical_coefficient(self, column: ExactRouteColumn) -> float:
        return self.canonical_sign * self.coefficient(column)


def column_arc_counts(column: ExactRouteColumn) -> dict[tuple[str, str], int]:
    locations = tuple(stop.facility_id for stop in column.stops)
    return dict(Counter(zip(locations, locations[1:])))


def aggregate_arc_usage(
    columns: Iterable[ExactRouteColumn],
    values: Iterable[float],
) -> dict[tuple[str, str], float]:
    usage: dict[tuple[str, str], float] = {}
    for column, value in zip(columns, values):
        if abs(float(value)) <= 1.0e-12:
            continue
        for arc, count in column_arc_counts(column).items():
            usage[arc] = usage.get(arc, 0.0) + float(value) * count
    return usage


def choose_fractional_arc_branch(
    columns: Iterable[ExactRouteColumn],
    values: Iterable[float],
    *,
    tolerance: float = 1.0e-7,
) -> tuple[tuple[str, str], float] | None:
    candidates = [
        (arc, value)
        for arc, value in aggregate_arc_usage(columns, values).items()
        if abs(value - round(value)) > tolerance
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            abs(abs(item[1] - round(item[1])) - 0.5),
            item[0],
        ),
    )


class _MilpBuilder:
    def __init__(self) -> None:
        self.cost: list[float] = []
        self.lower: list[float] = []
        self.upper: list[float] = []
        self.integer: list[bool] = []
        self.rows: list[int] = []
        self.columns: list[int] = []
        self.values: list[float] = []
        self.row_lower: list[float] = []
        self.row_upper: list[float] = []

    def variable(
        self, *, cost: float = 0.0, lower: float = 0.0, upper: float = 1.0,
        integer: bool = True,
    ) -> int:
        index = len(self.cost)
        self.cost.append(float(cost))
        self.lower.append(float(lower))
        self.upper.append(float(upper))
        self.integer.append(bool(integer))
        return index

    def constraint(
        self,
        coefficients: Iterable[tuple[int, float]],
        lower: float,
        upper: float,
    ) -> None:
        row = len(self.row_lower)
        for column, value in coefficients:
            if value:
                self.rows.append(row)
                self.columns.append(column)
                self.values.append(float(value))
        self.row_lower.append(float(lower))
        self.row_upper.append(float(upper))


def solve_fullspace_rmp_lp(
    data: ProblemData,
    pool: EliteRoutePool,
    config: Q1MasterConfig | None = None,
) -> FullspaceRmpLPResult:
    """Solve the RMP LP with standard nonnegative columns and no artificial caps."""

    arrays = _build_pattern_arrays(data, pool, config or Q1MasterConfig())
    started = time.perf_counter()
    result = linprog(
        arrays.primary,
        A_eq=arrays.equality,
        b_eq=arrays.equality_rhs,
        bounds=(0.0, None),
        method="highs",
        options={"dual_feasibility_tolerance": 1.0e-9},
    )
    if not result.success or result.x is None:
        raise RuntimeError(f"RMP LP failed: status={result.status}, {result.message}")
    duals = {
        key: float(result.eqlin.marginals[index])
        for index, key in enumerate(arrays.group_keys)
    }
    reduced_costs = arrays.primary - arrays.equality.T @ result.eqlin.marginals
    return FullspaceRmpLPResult(
        objective=float(result.fun),
        demand_duals=duals,
        reduced_costs=np.asarray(reduced_costs, dtype=float),
        selected_values=np.asarray(result.x, dtype=float),
        elapsed_seconds=round(time.perf_counter() - started, 6),
    )


def initial_exact_columns(
    data: ProblemData,
    pool: EliteRoutePool,
    config: Q1MasterConfig | None = None,
) -> tuple[ExactRouteColumn, ...]:
    arrays = _build_pattern_arrays(data, pool, config or Q1MasterConfig())
    return tuple(
        ExactRouteColumn(
            column_id=column.column_id,
            base_airport=column.elite_route.route.base_airport,
            aircraft_type=column.elite_route.route.aircraft_type,
            stops=column.elite_route.route.stops,
            allocation_pattern=column.pattern,
            duration_minutes=column.elite_route.evaluation.total_aircraft_time_minutes,
            source="initial_elite_pool",
        )
        for column in arrays.columns
    )


def pricing_result_to_column(
    result: ExactPricingResult,
    *,
    source: str,
) -> ExactRouteColumn:
    if result.reduced_cost is None or not result.stops or not result.allocation_pattern:
        raise ValueError("Pricing result has no materializable column")
    identity = (
        result.base_airport,
        result.aircraft_type,
        tuple(
            (stop.facility_id, bool(stop.refuel), bool(stop.is_service))
            for stop in result.stops
        ),
        result.allocation_pattern,
    )
    payload = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return ExactRouteColumn(
        column_id="Q1X-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        base_airport=result.base_airport,
        aircraft_type=result.aircraft_type,
        stops=result.stops,
        allocation_pattern=result.allocation_pattern,
        duration_minutes=int(result.route_duration_minutes),
        source=source,
    )


def solve_exact_column_rmp_lp(
    data: ProblemData,
    columns: Iterable[ExactRouteColumn],
) -> FullspaceRmpLPResult:
    columns = tuple(columns)
    group_keys = tuple(sorted(data.q1_pools))
    group_index = {key: index for index, key in enumerate(group_keys)}
    rows: list[int] = []
    matrix_columns: list[int] = []
    values: list[float] = []
    for column_index, column in enumerate(columns):
        for origin, destination, count in column.allocation_pattern:
            key = (origin, destination)
            if key not in group_index:
                raise ValueError(f"Column covers unknown demand group: {key}")
            rows.append(group_index[key])
            matrix_columns.append(column_index)
            values.append(float(count))
    equality = coo_matrix(
        (values, (rows, matrix_columns)),
        shape=(len(group_keys), len(columns)),
    ).tocsr()
    rhs = np.asarray([data.q1_pools[key].quantity for key in group_keys], dtype=float)
    costs = np.asarray([column.duration_minutes for column in columns], dtype=float)
    started = time.perf_counter()
    result = linprog(
        costs,
        A_eq=equality,
        b_eq=rhs,
        bounds=(0.0, None),
        method="highs",
        options={"dual_feasibility_tolerance": 1.0e-9},
    )
    if not result.success or result.x is None:
        raise RuntimeError(f"Exact-column RMP failed: {result.status}, {result.message}")
    dual_vector = np.asarray(result.eqlin.marginals, dtype=float)
    return FullspaceRmpLPResult(
        objective=float(result.fun),
        demand_duals={key: float(dual_vector[index]) for index, key in enumerate(group_keys)},
        reduced_costs=np.asarray(costs - equality.T @ dual_vector, dtype=float),
        selected_values=np.asarray(result.x, dtype=float),
        elapsed_seconds=round(time.perf_counter() - started, 6),
    )
def allocation_reward(
    pattern: Iterable[tuple[str, str, int]],
    duals: Mapping[tuple[str, str], float],
) -> float:
    return sum(float(duals[(origin, destination)]) * int(count)
               for origin, destination, count in pattern)


def column_reduced_cost(
    duration_minutes: int,
    pattern: Iterable[tuple[str, str, int]],
    duals: Mapping[tuple[str, str], float],
) -> float:
    return float(duration_minutes) - allocation_reward(pattern, duals)


def branch_column_reduced_cost(
    duration_minutes: int,
    pattern: Iterable[tuple[str, str, int]],
    demand_duals: Mapping[tuple[str, str], float],
    column: ExactRouteColumn,
    branch_duals: Mapping[ArcBranchRow, float],
    *,
    route_cost_multiplier: float = 1.0,
) -> float:
    """Reduced cost for canonical branch rows alpha*x<=beta.

    SciPy/HiGHS reports minimization marginals lambda<=0 for these rows, so
    rc = multiplier*c - pi*a - sum(lambda*alpha).
    """

    return (
        float(route_cost_multiplier) * float(duration_minutes)
        - allocation_reward(pattern, demand_duals)
        - sum(
            float(dual) * row.canonical_coefficient(column)
            for row, dual in branch_duals.items()
        )
    )


def exact_pricing(
    data: ProblemData,
    duals: Mapping[tuple[str, str], float],
    base_airport: str,
    aircraft_type: str,
    *,
    candidate_nodes: Iterable[str] | None = None,
    max_landings: int | None = None,
    time_limit_seconds: float | None = None,
    output_flag: bool = False,
    stop_when_decided: bool = False,
    branch_duals: Mapping[ArcBranchRow, float] | None = None,
    route_cost_multiplier: float = 1.0,
) -> ExactPricingResult:
    """Globally solve one base/type allocated-sortie pricing subproblem."""

    import highspy

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

    aircraft = data.config.aircraft_types[aircraft_type]
    physics = LegPhysics(data.config, data.matrix)
    builder = _MilpBuilder()
    node_count = len(nodes)
    active_branch_duals = dict(branch_duals or {})
    if any(float(value) > 1.0e-7 for value in active_branch_duals.values()):
        raise ValueError("Canonical <= branch-row duals must be nonpositive")

    z = {
        (position, node): builder.variable()
        for position in range(landings)
        for node in nodes
    }
    q = {
        (position, node): builder.variable(
            upper=1.0 if node in data.config.refuel_facilities else 0.0
        )
        for position in range(landings)
        for node in nodes
    }
    y = {
        (position, left, right): builder.variable()
        for position in range(1, landings)
        for left in nodes
        for right in nodes
    }
    end = {
        (position, node): builder.variable()
        for position in range(landings)
        for node in nodes
    }
    visited = {node: builder.variable() for node in nodes}

    eligible_groups = tuple(
        key for key in sorted(data.q1_pools)
        if key[0] == "LAND" or key[0] == base_airport
        if key[1] in nodes
    )
    allocation = {
        key: builder.variable(
            cost=-float(duals.get(key, 0.0)),
            upper=float(min(data.q1_pools[key].quantity, aircraft.seats)),
        )
        for key in eligible_groups
    }
    arrival_fuel = {
        position: builder.variable(
            lower=0.0, upper=aircraft.tank_capacity_kg, integer=False
        )
        for position in range(landings)
    }
    departure_fuel = {
        position: builder.variable(
            lower=0.0, upper=aircraft.tank_capacity_kg, integer=False
        )
        for position in range(landings)
    }

    # Objective: official integer flight minutes + offshore dwell - dual reward.
    for node in nodes:
        builder.cost[z[(0, node)]] += route_cost_multiplier * physics.flight_minutes(
            aircraft_type, base_airport, node
        )
        for position in range(landings):
            builder.cost[z[(position, node)]] += (
                route_cost_multiplier * data.config.stop_without_refuel_minutes
            )
            builder.cost[q[(position, node)]] += route_cost_multiplier * (
                data.config.stop_with_refuel_minutes
                - data.config.stop_without_refuel_minutes
            )
            builder.cost[end[(position, node)]] += (
                route_cost_multiplier
                * physics.flight_minutes(aircraft_type, node, base_airport)
            )
    for position in range(1, landings):
        for left in nodes:
            for right in nodes:
                builder.cost[y[(position, left, right)]] += (
                    route_cost_multiplier
                    * physics.flight_minutes(aircraft_type, left, right)
                )

    # Branch term -lambda*s times every real traversal of the directed arc.
    # Start, inter-position, return and repeated traversals all use the same
    # coefficient definition as existing Master columns.
    for row, dual in active_branch_duals.items():
        traversal_cost = -float(dual) * row.canonical_sign
        left, right = row.arc
        if left == base_airport and right in nodes:
            builder.cost[z[(0, right)]] += traversal_cost
        if right == base_airport and left in nodes:
            for position in range(landings):
                builder.cost[end[(position, left)]] += traversal_cost
        if left in nodes and right in nodes:
            for position in range(1, landings):
                builder.cost[y[(position, left, right)]] += traversal_cost

    inf = highspy.kHighsInf
    # Exactly one first landing; later positions are optional and contiguous.
    builder.constraint(((z[(0, node)], 1.0) for node in nodes), 1.0, 1.0)
    for position in range(1, landings):
        builder.constraint(
            [(z[(position, node)], 1.0) for node in nodes]
            + [(z[(position - 1, node)], -1.0) for node in nodes],
            -inf,
            0.0,
        )
        for right in nodes:
            builder.constraint(
                [(y[(position, left, right)], 1.0) for left in nodes]
                + [(z[(position, right)], -1.0)],
                0.0,
                0.0,
            )
        for left in nodes:
            builder.constraint(
                [(y[(position, left, right)], 1.0) for right in nodes]
                + [(end[(position - 1, left)], 1.0), (z[(position - 1, left)], -1.0)],
                0.0,
                0.0,
            )
    for node in nodes:
        builder.constraint(
            [(end[(landings - 1, node)], 1.0), (z[(landings - 1, node)], -1.0)],
            0.0,
            0.0,
        )

    # Exact visited-node OR and legal refuel linkage. Repeats remain allowed.
    for node in nodes:
        for position in range(landings):
            builder.constraint(
                [(z[(position, node)], 1.0), (visited[node], -1.0)],
                -inf,
                0.0,
            )
            builder.constraint(
                [(q[(position, node)], 1.0), (z[(position, node)], -1.0)],
                -inf,
                0.0,
            )
        builder.constraint(
            [(visited[node], 1.0)]
            + [(z[(position, node)], -1.0) for position in range(landings)],
            -inf,
            0.0,
        )

    # Exact unit-weight bounded allocation. LAND may use every base; fixed OD only its base.
    builder.constraint(((index, 1.0) for index in allocation.values()), 1.0, aircraft.seats)
    for key, index in allocation.items():
        destination = key[1]
        builder.constraint(
            [(index, 1.0), (visited[destination], -data.q1_pools[key].quantity)],
            -inf,
            0.0,
        )

    tank = aircraft.tank_capacity_kg
    reserve = aircraft.reserve_kg
    maximum_burn = max(
        physics.fuel_for_leg(aircraft_type, left, right)
        for left in (base_airport, *nodes)
        for right in (*nodes, base_airport)
    )
    big_m = 2.0 * tank + maximum_burn

    # First-leg fuel is exact from a full tank.
    builder.constraint(
        [(arrival_fuel[0], 1.0)]
        + [
            (z[(0, node)], physics.fuel_for_leg(aircraft_type, base_airport, node))
            for node in nodes
        ],
        tank,
        tank,
    )
    for position in range(1, landings):
        transition_expression = (
            [(arrival_fuel[position], 1.0), (departure_fuel[position - 1], -1.0)]
            + [
                (
                    y[(position, left, right)],
                    physics.fuel_for_leg(aircraft_type, left, right),
                )
                for left in nodes
                for right in nodes
            ]
        )
        active = [(z[(position, node)], big_m) for node in nodes]
        builder.constraint(transition_expression + active, -inf, big_m)
        builder.constraint(
            transition_expression
            + [(z[(position, node)], -big_m) for node in nodes],
            -big_m,
            inf,
        )

    for position in range(landings):
        active = [(z[(position, node)], 1.0) for node in nodes]
        refuel = [(q[(position, node)], 1.0) for node in nodes]
        builder.constraint(
            [(arrival_fuel[position], 1.0)]
            + [(index, -reserve) for index, _ in active],
            0.0,
            inf,
        )
        builder.constraint(
            [(arrival_fuel[position], 1.0)]
            + [(index, -tank) for index, _ in active],
            -inf,
            0.0,
        )
        builder.constraint(
            [(departure_fuel[position], 1.0)]
            + [(index, -reserve) for index, _ in active],
            0.0,
            inf,
        )
        builder.constraint(
            [(departure_fuel[position], 1.0)]
            + [(index, -tank) for index, _ in active],
            -inf,
            0.0,
        )
        builder.constraint(
            [(departure_fuel[position], 1.0), (arrival_fuel[position], -1.0)],
            0.0,
            inf,
        )
        builder.constraint(
            [(departure_fuel[position], 1.0), (arrival_fuel[position], -1.0)]
            + [(index, -big_m) for index, _ in refuel],
            -inf,
            0.0,
        )
        builder.constraint(
            [(departure_fuel[position], 1.0)]
            + [(index, -tank) for index, _ in refuel],
            0.0,
            inf,
        )
        for node in nodes:
            required = reserve + physics.fuel_for_leg(
                aircraft_type, node, base_airport
            )
            builder.constraint(
                [(departure_fuel[position], 1.0), (end[(position, node)], -required)],
                0.0,
                inf,
            )

    matrix = coo_matrix(
        (builder.values, (builder.rows, builder.columns)),
        shape=(len(builder.row_lower), len(builder.cost)),
    ).tocsc()
    lp = highspy.HighsLp()
    lp.num_col_ = len(builder.cost)
    lp.num_row_ = len(builder.row_lower)
    lp.col_cost_ = np.asarray(builder.cost)
    lp.col_lower_ = np.asarray(builder.lower)
    lp.col_upper_ = np.asarray(builder.upper)
    lp.row_lower_ = np.asarray(builder.row_lower)
    lp.row_upper_ = np.asarray(builder.row_upper)
    lp.integrality_ = [
        highspy.HighsVarType.kInteger if integer else highspy.HighsVarType.kContinuous
        for integer in builder.integer
    ]
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.num_col_ = lp.num_col_
    lp.a_matrix_.num_row_ = lp.num_row_
    lp.a_matrix_.start_ = matrix.indptr.astype(np.int32)
    lp.a_matrix_.index_ = matrix.indices.astype(np.int32)
    lp.a_matrix_.value_ = matrix.data.astype(float)

    highs = highspy.Highs()
    if highs.passModel(lp) != highspy.HighsStatus.kOk:
        raise RuntimeError("HiGHS rejected exact pricing model")
    highs.setOptionValue("output_flag", output_flag)
    highs.setOptionValue("presolve", "on")
    highs.setOptionValue("mip_rel_gap", 0.0)
    highs.setOptionValue("mip_abs_gap", 1.0e-9)
    highs.setOptionValue("mip_feasibility_tolerance", 1.0e-8)
    highs.setOptionValue("primal_feasibility_tolerance", 1.0e-8)
    highs.setOptionValue("dual_feasibility_tolerance", 1.0e-8)
    if time_limit_seconds is not None:
        highs.setOptionValue("time_limit", float(time_limit_seconds))
    if stop_when_decided:
        def stop_on_certificate(event) -> None:
            lower_bound = float(event.data_out.mip_dual_bound)
            incumbent = float(event.data_out.mip_primal_bound)
            if (
                math.isfinite(lower_bound)
                and lower_bound >= -PRICING_TOL
            ) or (
                math.isfinite(incumbent)
                and incumbent < -PRICING_TOL
            ):
                event.interrupt()

        highs.cbMipLogging += stop_on_certificate
        highs.cbMipImprovingSolution += stop_on_certificate
    started = time.perf_counter()
    run_status = highs.run()
    elapsed = time.perf_counter() - started
    status = highs.getModelStatus()
    info = highs.getInfo()
    proven = status == highspy.HighsModelStatus.kOptimal
    solution = highs.getSolution()
    if not solution.value_valid:
        return ExactPricingResult(
            base_airport, aircraft_type, highs.modelStatusToString(status), proven,
            None, None, None, (), (), int(info.mip_node_count),
            float(info.mip_dual_bound) if math.isfinite(info.mip_dual_bound) else None,
            round(elapsed, 6), node_count, landings, False,
            bool(
                math.isfinite(info.mip_dual_bound)
                and info.mip_dual_bound >= -PRICING_TOL
            ),
            False,
        )

    values = np.asarray(solution.col_value)
    chosen_nodes: list[str] = []
    chosen_refuel: list[bool] = []
    for position in range(landings):
        selected = [node for node in nodes if values[z[(position, node)]] > 0.5]
        if not selected:
            break
        node = selected[0]
        chosen_nodes.append(node)
        chosen_refuel.append(bool(values[q[(position, node)]] > 0.5))
    pattern = tuple(
        (origin, destination, int(round(values[index])))
        for (origin, destination), index in allocation.items()
        if values[index] > 0.5
    )
    service_destinations = {destination for _, destination, _ in pattern}
    first_service: set[str] = set()
    route_stops = [RouteStop(base_airport)]
    for node, refuel_flag in zip(chosen_nodes, chosen_refuel):
        is_service = node in service_destinations and node not in first_service
        if is_service:
            first_service.add(node)
        route_stops.append(RouteStop(node, refuel=refuel_flag, is_service=is_service))
    route_stops.append(RouteStop(base_airport))
    service_order: list[str] = []
    seen_service: set[str] = set()
    for node in chosen_nodes:
        if node in service_destinations and node not in seen_service:
            seen_service.add(node)
            service_order.append(node)
    empty_route = RoutePlan(
        base_airport,
        aircraft_type,
        tuple(route_stops),
        service_facilities=tuple(service_order),
    )
    evaluation = evaluate_route(empty_route, matrix=data.matrix, config=data.config)
    if not evaluation.feasible:
        raise RuntimeError(f"Exact pricing produced illegal route: {evaluation.issues}")
    reward = allocation_reward(pattern, duals)
    priced_column = ExactRouteColumn(
        column_id="pricing-verification",
        base_airport=base_airport,
        aircraft_type=aircraft_type,
        stops=tuple(route_stops),
        allocation_pattern=pattern,
        duration_minutes=evaluation.total_aircraft_time_minutes,
        source="pricing-verification",
    )
    reduced_cost = branch_column_reduced_cost(
        evaluation.total_aircraft_time_minutes,
        pattern,
        duals,
        priced_column,
        active_branch_duals,
        route_cost_multiplier=route_cost_multiplier,
    )
    if abs(reduced_cost - float(info.objective_function_value)) > 5.0e-6:
        raise RuntimeError(
            "Pricing objective disagrees with shared evaluator: "
            f"model={info.objective_function_value}, shared={reduced_cost}"
        )
    dual_bound = float(info.mip_dual_bound)
    if not math.isfinite(dual_bound):
        dual_bound = None
    return ExactPricingResult(
        base_airport=base_airport,
        aircraft_type=aircraft_type,
        status=highs.modelStatusToString(status),
        proven_optimal=proven,
        reduced_cost=float(reduced_cost),
        route_duration_minutes=evaluation.total_aircraft_time_minutes,
        allocation_reward=float(reward),
        allocation_pattern=pattern,
        stops=tuple(route_stops),
        node_count=int(info.mip_node_count),
        dual_bound=dual_bound,
        elapsed_seconds=round(elapsed, 6),
        candidate_nodes=node_count,
        max_landings=landings,
        repeated_visit=len(chosen_nodes) != len(set(chosen_nodes)),
        certified_no_negative_column=bool(
            proven and reduced_cost >= -PRICING_TOL
            or dual_bound is not None and dual_bound >= -PRICING_TOL
        ),
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
