from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Iterable

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from ..rules import flight_minutes
from .data import ProblemData
from .q3 import Q3Person, Q3Variant, _assignment_for_person


@dataclass(frozen=True)
class LowerBoundResult:
    name: str
    valid_for_original_problem: bool
    objective_minutes_continuous: float
    objective_minutes_integer_ceiling: int
    solver_status: int
    solver_message: str
    runtime_seconds: float
    variables: int
    equality_constraints: int
    inequality_constraints: int
    details: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _mandatory_od_demands(people: Iterable[Q3Person]) -> Counter[tuple[str, str]]:
    return Counter(person.od for person in people if person.mandatory)


def layered_multicommodity_flow_bound(
    people: Iterable[Q3Person],
    data: ProblemData,
    *,
    time_limit_seconds: float = 300.0,
) -> LowerBoundResult:
    """A global lower bound from a continuous aircraft/passenger-flow relaxation.

    Aircraft flow is disaggregated by home airport and type and must be balanced
    at every node. Passenger commodities may split and transfer; aircraft flows
    are continuous; fuel, integer sorties, time windows and turnaround are
    relaxed. Every original Q3 schedule maps to a feasible point of this LP, so
    its optimum is a valid lower bound for the original first-stage objective.
    """
    started = time.perf_counter()
    config = data.config
    airports = tuple(config.airports)
    facilities = tuple(config.facilities)
    physical_nodes = airports + facilities
    land_node = "LAND"

    # Passenger arcs are exactly those that some home-airport aircraft layer
    # may fly: airport<->facility and facility<->facility. Airport transfers
    # are deliberately excluded.
    physical_arcs: list[tuple[str, str]] = []
    for airport in airports:
        for facility in facilities:
            physical_arcs.append((airport, facility))
            physical_arcs.append((facility, airport))
    for origin in facilities:
        for destination in facilities:
            if origin != destination:
                physical_arcs.append((origin, destination))
    land_arcs = [(land_node, airport) for airport in airports] + [
        (airport, land_node) for airport in airports
    ]
    passenger_arcs = physical_arcs + land_arcs

    # One continuous aircraft-circulation layer per home-airport/type pair.
    x_arcs: list[tuple[str, str, str, str, int]] = []
    arc_to_x: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    layer_to_x: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    layer_nodes: dict[tuple[str, str], tuple[str, ...]] = {}
    for airport in airports:
        for aircraft_type, aircraft in config.aircraft_types.items():
            layer = (airport, aircraft_type)
            nodes = (airport,) + facilities
            layer_nodes[layer] = nodes
            for origin in nodes:
                for destination in nodes:
                    if origin == destination:
                        continue
                    duration = flight_minutes(
                        float(data.matrix[origin][destination]), aircraft.speed_kmh
                    ) + (10 if destination in config.facilities else 0)
                    column = len(x_arcs)
                    x_arcs.append(
                        (airport, aircraft_type, origin, destination, duration)
                    )
                    arc_to_x[(origin, destination)].append(
                        (column, aircraft.seats)
                    )
                    layer_to_x[layer].append((column, duration))

    demands = _mandatory_od_demands(people)
    destination_supply: dict[str, Counter[str]] = defaultdict(Counter)
    for (origin, destination), demand in demands.items():
        destination_supply[destination][origin] += demand
    commodities = sorted(destination_supply)
    x_count = len(x_arcs)
    passenger_arc_count = len(passenger_arcs)
    variable_count = x_count + len(commodities) * passenger_arc_count

    objective = np.zeros(variable_count, dtype=float)
    for column, (*_, duration) in enumerate(x_arcs):
        objective[column] = float(duration)
    lower_bounds = np.zeros(variable_count, dtype=float)
    upper_bounds = np.full(variable_count, np.inf, dtype=float)

    def f_column(commodity_index: int, arc_index: int) -> int:
        return x_count + commodity_index * passenger_arc_count + arc_index

    # LAND is a bookkeeping supernode. It may only inject passenger flow for a
    # LAND origin or absorb flow for a LAND destination.
    for commodity_index, destination in enumerate(commodities):
        land_offset = len(physical_arcs)
        for local_index, (left, _right) in enumerate(land_arcs):
            allowed = (destination_supply[destination][land_node] > 0 and left == land_node) or (
                destination == land_node and left in airports
            )
            if not allowed:
                upper_bounds[
                    f_column(commodity_index, land_offset + local_index)
                ] = 0.0

    # Equalities: aircraft flow conservation in every layer and passenger-flow
    # conservation for every commodity and node.
    eq_rows: list[int] = []
    eq_cols: list[int] = []
    eq_values: list[float] = []
    eq_rhs: list[float] = []
    row = 0
    for layer, nodes in layer_nodes.items():
        for node in nodes:
            for column, _duration in layer_to_x[layer]:
                _airport, _aircraft_type, origin, destination, _ = x_arcs[column]
                if origin == node:
                    eq_rows.append(row)
                    eq_cols.append(column)
                    eq_values.append(1.0)
                if destination == node:
                    eq_rows.append(row)
                    eq_cols.append(column)
                    eq_values.append(-1.0)
            eq_rhs.append(0.0)
            row += 1
    all_passenger_nodes = physical_nodes + (land_node,)
    for commodity_index, destination in enumerate(commodities):
        total_demand = float(sum(destination_supply[destination].values()))
        for node in all_passenger_nodes:
            for arc_index, (left, right) in enumerate(passenger_arcs):
                if left == node:
                    eq_rows.append(row)
                    eq_cols.append(f_column(commodity_index, arc_index))
                    eq_values.append(1.0)
                if right == node:
                    eq_rows.append(row)
                    eq_cols.append(f_column(commodity_index, arc_index))
                    eq_values.append(-1.0)
            supply = float(destination_supply[destination][node])
            eq_rhs.append(supply - total_demand if node == destination else supply)
            row += 1
    equality_count = row
    equality = coo_matrix(
        (eq_values, (eq_rows, eq_cols)),
        shape=(equality_count, variable_count),
    ).tocsr()

    # Inequalities: passenger load on each directed physical arc cannot exceed
    # the total seats supplied by all compatible aircraft layers. The final
    # nine rows impose the valid seven-day aggregate availability of each
    # home-airport/type fleet (14 operating hours per aircraft per day).
    ub_rows: list[int] = []
    ub_cols: list[int] = []
    ub_values: list[float] = []
    ub_rhs: list[float] = []
    row = 0
    for arc_index, arc in enumerate(physical_arcs):
        for column, seats in arc_to_x[arc]:
            ub_rows.append(row)
            ub_cols.append(column)
            ub_values.append(-float(seats))
        for commodity_index in range(len(commodities)):
            ub_rows.append(row)
            ub_cols.append(f_column(commodity_index, arc_index))
            ub_values.append(1.0)
        ub_rhs.append(0.0)
        row += 1
    operating_minutes_per_aircraft = 7 * 14 * 60
    for layer, columns in layer_to_x.items():
        for column, duration in columns:
            ub_rows.append(row)
            ub_cols.append(column)
            ub_values.append(float(duration))
        airport, aircraft_type = layer
        ub_rhs.append(
            float(
                config.fleet_counts[airport][aircraft_type]
                * operating_minutes_per_aircraft
            )
        )
        row += 1
    inequality_count = row
    inequality = coo_matrix(
        (ub_values, (ub_rows, ub_cols)),
        shape=(inequality_count, variable_count),
    ).tocsr()

    result = linprog(
        objective,
        A_ub=inequality,
        b_ub=np.asarray(ub_rhs),
        A_eq=equality,
        b_eq=np.asarray(eq_rhs),
        bounds=np.column_stack((lower_bounds, upper_bounds)),
        method="highs",
        options={"time_limit": float(time_limit_seconds), "presolve": True},
    )
    if result.x is None or not math.isfinite(float(result.fun)):
        raise RuntimeError(
            f"Q3 multicommodity lower-bound LP failed: {result.status}, {result.message}"
        )
    value = float(result.fun)
    return LowerBoundResult(
        name="layered_continuous_multicommodity_flow",
        valid_for_original_problem=True,
        objective_minutes_continuous=value,
        objective_minutes_integer_ceiling=math.ceil(value - 1e-8),
        solver_status=int(result.status),
        solver_message=str(result.message),
        runtime_seconds=round(time.perf_counter() - started, 6),
        variables=variable_count,
        equality_constraints=equality_count,
        inequality_constraints=inequality_count,
        details={
            "mandatory_people": int(sum(demands.values())),
            "destination_aggregated_commodities": len(commodities),
            "aircraft_layers": len(layer_nodes),
            "aircraft_arc_variables": x_count,
            "passenger_arc_variables": len(commodities) * passenger_arc_count,
            "relaxed_features": [
                "continuous aircraft flow",
                "split passenger flow and transfers",
                "integer sorties",
                "fuel and refuelling",
                "time windows and daily assignment",
                "turnaround and no-overlap",
                "maximum sea-stop count",
            ],
        },
    )


def candidate_route_master_lp_bound(
    people: Iterable[Q3Person],
    variants_by_od: dict[tuple[str, str], tuple[Q3Variant, ...]],
    data: ProblemData,
    *,
    time_limit_seconds: float = 300.0,
) -> LowerBoundResult:
    """LP bound for the finite cached route pool (not a global proof bound)."""
    started = time.perf_counter()
    people_list = [person for person in people if person.mandatory]
    demands = _mandatory_od_demands(people_list)
    ods = sorted(demands)
    unique = {
        variant.key: variant
        for values in variants_by_od.values()
        for variant in values
    }
    variants = sorted(unique.values(), key=lambda variant: variant.key)
    route_index = {variant.key: index for index, variant in enumerate(variants)}

    assignments: list[tuple[int, int, int, int]] = []
    by_od: dict[int, list[int]] = defaultdict(list)
    by_route_leg: dict[tuple[int, int], list[int]] = defaultdict(list)
    for od_index, od in enumerate(ods):
        representative = next(person for person in people_list if person.od == od)
        for variant in variants_by_od[od]:
            interval = _assignment_for_person(representative, variant, data.config)
            if interval is None:
                continue
            route = route_index[variant.key]
            pickup, delivery = interval
            column = len(variants) + len(assignments)
            assignments.append((od_index, route, pickup, delivery))
            by_od[od_index].append(column)
            for leg in range(pickup, delivery):
                by_route_leg[(route, leg)].append(column)

    variable_count = len(variants) + len(assignments)
    objective = np.zeros(variable_count)
    for index, variant in enumerate(variants):
        objective[index] = float(variant.duration)

    eq_rows: list[int] = []
    eq_cols: list[int] = []
    eq_values: list[float] = []
    eq_rhs: list[float] = []
    for od_index, od in enumerate(ods):
        for column in by_od[od_index]:
            eq_rows.append(od_index)
            eq_cols.append(column)
            eq_values.append(1.0)
        eq_rhs.append(float(demands[od]))
    equality = coo_matrix(
        (eq_values, (eq_rows, eq_cols)),
        shape=(len(ods), variable_count),
    ).tocsr()

    ub_rows: list[int] = []
    ub_cols: list[int] = []
    ub_values: list[float] = []
    ub_rhs: list[float] = []
    row = 0
    for (route, _leg), columns in sorted(by_route_leg.items()):
        ub_rows.append(row)
        ub_cols.append(route)
        ub_values.append(-float(variants[route].capacity))
        for column in columns:
            ub_rows.append(row)
            ub_cols.append(column)
            ub_values.append(1.0)
        ub_rhs.append(0.0)
        row += 1
    inequality = coo_matrix(
        (ub_values, (ub_rows, ub_cols)),
        shape=(row, variable_count),
    ).tocsr()
    result = linprog(
        objective,
        A_ub=inequality,
        b_ub=np.asarray(ub_rhs),
        A_eq=equality,
        b_eq=np.asarray(eq_rhs),
        bounds=(0.0, None),
        method="highs",
        options={"time_limit": float(time_limit_seconds), "presolve": True},
    )
    if result.x is None or not math.isfinite(float(result.fun)):
        raise RuntimeError(
            f"Q3 candidate-route LP failed: {result.status}, {result.message}"
        )
    value = float(result.fun)
    equality_duals = list(getattr(result.eqlin, "marginals", []))
    inequality_duals = list(getattr(result.ineqlin, "marginals", []))
    lower_reduced_costs = list(getattr(result.lower, "marginals", []))
    top_od_duals = sorted(
        (
            {
                "od": list(od),
                "demand": int(demands[od]),
                "dual_minutes": float(equality_duals[index]),
            }
            for index, od in enumerate(ods)
        ),
        key=lambda row: -abs(float(row["dual_minutes"])),
    )[:25]
    selected_routes = [
        {
            "route_key": repr(variant.key),
            "multiplicity": float(result.x[index]),
            "duration": variant.duration,
        }
        for index, variant in enumerate(variants)
        if result.x[index] > 1e-8
    ]
    return LowerBoundResult(
        name="finite_candidate_route_master_lp",
        valid_for_original_problem=False,
        objective_minutes_continuous=value,
        objective_minutes_integer_ceiling=math.ceil(value - 1e-8),
        solver_status=int(result.status),
        solver_message=str(result.message),
        runtime_seconds=round(time.perf_counter() - started, 6),
        variables=variable_count,
        equality_constraints=len(ods),
        inequality_constraints=row,
        details={
            "scope": "finite cached route pool only; not a global lower bound",
            "mandatory_people": int(sum(demands.values())),
            "od_count": len(ods),
            "route_variants": len(variants),
            "compatible_od_route_assignments": len(assignments),
            "selected_route_columns": len(selected_routes),
            "top_selected_routes": sorted(
                selected_routes,
                key=lambda row: -float(row["multiplicity"]),
            )[:25],
            "top_od_duals": top_od_duals,
            "capacity_shadow_rows_nonzero": sum(
                abs(float(value)) > 1e-9 for value in inequality_duals
            ),
            "negative_reduced_cost_columns": sum(
                float(value) < -1e-7 for value in lower_reduced_costs
            ),
            "duals_are_restricted_master_only": True,
            "relaxed_features": [
                "continuous route multiplicity",
                "time windows and daily assignment",
                "concrete aircraft no-overlap",
                "integer passenger assignment",
            ],
        },
    )
