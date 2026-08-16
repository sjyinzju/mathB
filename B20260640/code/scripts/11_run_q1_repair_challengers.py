from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.io_utils import write_json
from src.solver import (
    Q1ALNSConfig,
    SolverCache,
    exact_targeted_repair,
    load_problem_data,
    load_q1_solution,
    route_elimination_audit,
    targeted_route_indices,
)
from src.solver.alns import RouteVariant, _variant_pool
from src.solver.evaluator import evaluate_route
from src.solver.models import PassengerAssignment, RoutePlan, Solution, aggregate_evaluations


@dataclass
class _HeuristicState:
    remaining: dict[tuple[str, str], list[PassengerAssignment]]
    routes: list[RoutePlan]
    evaluations: list


def _copy_remaining(groups):
    return {key: list(values) for key, values in groups.items()}


def _eligible_keys(variant: RouteVariant, remaining):
    return [
        key
        for key, people in remaining.items()
        if people
        and key[1] in variant.service_order
        and (key[0] == "LAND" or key[0] == variant.base_airport)
    ]


def _variant_score(variant: RouteVariant, remaining) -> float | None:
    eligible = _eligible_keys(variant, remaining)
    for destination in variant.service_order:
        if not any(key[1] == destination for key in eligible):
            return None
    possible_load = min(
        variant.capacity,
        sum(len(remaining[key]) for key in eligible),
    )
    if possible_load <= 0:
        return None
    return (
        variant.evaluation.total_aircraft_time_minutes / possible_load
        + 1.0e-4 * sum(variant.arrival_minutes.values())
    )


def _choose_group_and_variants(remaining, variants, regret_k: int):
    best_choice = None
    for group_key, people in remaining.items():
        if not people:
            continue
        compatible = []
        for variant in variants:
            if group_key[1] not in variant.service_order:
                continue
            if group_key[0] != "LAND" and group_key[0] != variant.base_airport:
                continue
            score = _variant_score(variant, remaining)
            if score is not None:
                compatible.append((score, variant))
        compatible.sort(key=lambda item: (item[0], item[1].key))
        if not compatible:
            return group_key, []
        kth = compatible[min(regret_k - 1, len(compatible) - 1)][0]
        regret = kth - compatible[0][0]
        choice = (-regret, compatible[0][0], -len(people), group_key)
        if best_choice is None or choice < best_choice[0]:
            best_choice = (choice, group_key, compatible)
    if best_choice is None:
        return None, []
    return best_choice[1], [variant for _, variant in best_choice[2]]


def _apply_variant(variant: RouteVariant, remaining, data):
    updated = _copy_remaining(remaining)
    chosen: list[PassengerAssignment] = []
    for destination in variant.service_order:
        keys = [
            key
            for key in _eligible_keys(variant, updated)
            if key[1] == destination
        ]
        if not keys:
            return None
        key = max(keys, key=lambda item: (len(updated[item]), item))
        chosen.append(updated[key].pop())
    while len(chosen) < variant.capacity:
        keys = _eligible_keys(variant, updated)
        if not keys:
            break
        key = max(keys, key=lambda item: (len(updated[item]), item))
        chosen.append(updated[key].pop())
    locations = tuple(stop.facility_id for stop in variant.route.stops)
    assignments = tuple(
        PassengerAssignment(
            person.person_id,
            person.origin_id,
            person.destination_id,
            0,
            locations.index(person.destination_id, 1),
        )
        for person in sorted(chosen, key=lambda item: item.person_id)
    )
    route = RoutePlan(
        variant.base_airport,
        variant.aircraft_type,
        variant.route.stops,
        assignments,
        variant.service_order,
    )
    evaluation = evaluate_route(route, matrix=data.matrix, config=data.config)
    if not evaluation.feasible:
        return None
    return updated, route, evaluation


def _greedy_complete(state, variants, data, regret_k: int):
    state = _HeuristicState(
        _copy_remaining(state.remaining), list(state.routes), list(state.evaluations)
    )
    safety = 0
    while any(state.remaining.values()):
        safety += 1
        if safety > 200:
            return None
        _, ordered = _choose_group_and_variants(state.remaining, variants, regret_k)
        if not ordered:
            return None
        applied = next(
            (
                result
                for variant in ordered
                if (result := _apply_variant(variant, state.remaining, data))
                is not None
            ),
            None,
        )
        if applied is None:
            return None
        state.remaining, route, evaluation = applied
        state.routes.append(route)
        state.evaluations.append(evaluation)
    return state


def _beam_complete(initial, variants, data, width: int = 4, depth: int = 2):
    beam = [initial]
    for _ in range(depth):
        expanded = []
        for state in beam:
            if not any(state.remaining.values()):
                expanded.append(state)
                continue
            _, ordered = _choose_group_and_variants(state.remaining, variants, 3)
            for variant in ordered[:width]:
                applied = _apply_variant(variant, state.remaining, data)
                if applied is None:
                    continue
                remaining, route, evaluation = applied
                expanded.append(
                    _HeuristicState(
                        remaining,
                        [*state.routes, route],
                        [*state.evaluations, evaluation],
                    )
                )
        expanded.sort(
            key=lambda state: (
                sum(item.total_aircraft_time_minutes for item in state.evaluations)
                + sum(len(people) for people in state.remaining.values()) * 8,
                len(state.routes),
            )
        )
        beam = expanded[:width]
        if not beam:
            return None
    completed = [
        result
        for state in beam
        if (result := _greedy_complete(state, variants, data, 3)) is not None
    ]
    if not completed:
        return None
    return min(
        completed,
        key=lambda state: (
            sum(item.total_aircraft_time_minutes for item in state.evaluations),
            sum(item.total_passenger_travel_time_minutes for item in state.evaluations),
            len(state.routes),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Q1 bounded Regret-k/Beam/CP-SAT repair challengers")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--start-dir", type=Path, required=True)
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "outputs" / "q1" / "final-or"
    )
    args = parser.parse_args()

    run_dir = args.output_root / args.run_id
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    data = load_problem_data()
    solution = load_q1_solution(
        args.start_dir / "q1-routes.csv",
        args.start_dir / "q1-assignments.csv",
        data,
        method="q1_repair_challenger_start",
    )
    audit = route_elimination_audit(solution, data)
    indices = targeted_route_indices(
        solution, data, int(audit[0]["route_index"]), 6, mode="high_impact"
    )
    destroyed = [solution.routes[index] for index in indices]
    destroyed_evaluations = [
        evaluate_route(route, matrix=data.matrix, config=data.config)
        for route in destroyed
    ]
    groups = defaultdict(list)
    for route in destroyed:
        for assignment in route.assignments:
            groups[(assignment.origin_id, assignment.destination_id)].append(assignment)
    for people in groups.values():
        people.sort(key=lambda person: person.person_id)
    solver_cache = SolverCache(data)
    config = Q1ALNSConfig(
        iterations=1,
        time_limit_seconds=20.0,
        min_destroy_routes=2,
        max_destroy_routes=6,
        max_service_nodes=2,
        max_long_service_orders=80,
        repair_time_limit_seconds=10.0,
    )
    pool = _variant_pool(
        destroyed,
        destroyed_evaluations,
        groups,
        data,
        config,
        {},
        solver_cache,
    )
    initial_state = _HeuristicState(_copy_remaining(groups), [], [])
    results = []
    candidates: list[tuple[str, _HeuristicState]] = []
    for regret_k in (2, 3, 4):
        started = time.perf_counter()
        state = _greedy_complete(initial_state, pool.variants, data, regret_k)
        elapsed = time.perf_counter() - started
        if state is not None:
            candidates.append((f"regret_{regret_k}", state))
        results.append(
            {
                "method": f"Regret-{regret_k}",
                "feasible": state is not None,
                "local_aircraft_time": sum(
                    item.total_aircraft_time_minutes for item in state.evaluations
                )
                if state is not None
                else None,
                "local_routes": len(state.routes) if state is not None else None,
                "elapsed_seconds": round(elapsed, 6),
            }
        )
    started = time.perf_counter()
    beam = _beam_complete(initial_state, pool.variants, data)
    beam_elapsed = time.perf_counter() - started
    if beam is not None:
        candidates.append(("beam_4x2", beam))
    results.append(
        {
            "method": "Beam width=4 depth=2 + Regret-3 completion",
            "feasible": beam is not None,
            "local_aircraft_time": sum(
                item.total_aircraft_time_minutes for item in beam.evaluations
            )
            if beam is not None
            else None,
            "local_routes": len(beam.routes) if beam is not None else None,
            "elapsed_seconds": round(beam_elapsed, 6),
        }
    )
    started = time.perf_counter()
    milp = exact_targeted_repair(
        solution,
        data,
        indices,
        reason="p2_milp_control",
        max_service_nodes=2,
        max_long_service_orders=80,
        repair_time_limit_seconds=10.0,
        cache=solver_cache,
    )
    results.append(
        {
            "method": "MILP exact repair control",
            "feasible": milp.solution is not None,
            "local_aircraft_time": milp.aircraft_time_after,
            "local_routes": milp.routes_after,
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        }
    )

    kept = [
        route for index, route in enumerate(solution.routes) if index not in set(indices)
    ]
    kept_evaluations = [
        evaluate_route(route, matrix=data.matrix, config=data.config) for route in kept
    ]
    full_candidates = []
    for name, state in candidates:
        full = Solution(
            routes=tuple([*kept, *state.routes]),
            metrics=aggregate_evaluations(
                [*kept_evaluations, *state.evaluations], data.q1_passenger_count
            ),
            method=f"q1_{name}_challenger",
        )
        full_candidates.append(
            {"method": name, "metrics": full.metrics.to_dict()}
        )
    cp_sat_available = importlib.util.find_spec("ortools") is not None
    write_json(
        run_dir / "challenger-results.json",
        {
            "start_metrics": solution.metrics.to_dict(),
            "route_indices": list(indices),
            "destroyed_routes": len(indices),
            "destroyed_passengers": sum(route.passenger_count for route in destroyed),
            "candidate_variants": len(pool.variants),
            "local_control_time": sum(
                evaluation.total_aircraft_time_minutes
                for evaluation in destroyed_evaluations
            ),
            "results": results,
            "full_candidates": full_candidates,
            "cp_sat": {
                "available": cp_sat_available,
                "decision": "REJECT"
                if not cp_sat_available
                else "OPTIONAL_NOT_ADOPTED",
                "reason": (
                    "OR-Tools is not a project dependency; the shared MILP control is feasible, "
                    "validated elsewhere, and backend proliferation is not justified."
                    if not cp_sat_available
                    else "No evidence that a second exact backend would address the measured bottleneck."
                ),
            },
        },
    )
    print(
        "Q1 REPAIR CHALLENGERS PASS: "
        + ", ".join(
            f"{row['method']}={row['local_aircraft_time']}"
            for row in results
        )
        + f", cp_sat_available={cp_sat_available}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
