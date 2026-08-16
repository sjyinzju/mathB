from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from itertools import permutations
from typing import Sequence

from .data import ProblemData
from .models import RoutePlan
from .q2 import candidate_service_sequences


@dataclass(frozen=True)
class Q2SequenceFeatures:
    sequence: tuple[str, ...]
    route_distance_km: float
    detour_ratio: float
    directed_shuttle_flow: int
    reverse_shuttle_flow: int
    bidirectional_flow: int
    outbound_flow: int
    inbound_flow: int
    flow_complementarity: int
    seat_reuse_proxy: int
    fixed_airport_affinity: int
    land_flexible_flow: int
    capacity_fit: float
    refuel_facilities: int
    technical_stop_complexity_proxy: float
    score: float

    def to_dict(self) -> dict[str, object]:
        values = asdict(self)
        values["sequence"] = list(self.sequence)
        return values


@dataclass(frozen=True)
class Q2DirectedFlowGraph:
    nodes: tuple[str, ...]
    directed_demand: dict[tuple[str, str], int]
    shuttle_demand: dict[tuple[str, str], int]
    fixed_airport_demand: dict[tuple[str, str], int]
    land_outbound: dict[str, int]
    land_inbound: dict[str, int]
    in_flow: dict[str, int]
    out_flow: dict[str, int]
    net_flow: dict[str, int]
    bidirectional_flow: dict[tuple[str, str], int]
    airport_affinity: dict[tuple[str, str], int]

    def summary(self) -> dict[str, object]:
        offshore = [node for node in self.nodes if node.startswith("F")]
        top_out = sorted(
            ((node, self.out_flow.get(node, 0)) for node in offshore),
            key=lambda item: (-item[1], item[0]),
        )[:10]
        top_in = sorted(
            ((node, self.in_flow.get(node, 0)) for node in offshore),
            key=lambda item: (-item[1], item[0]),
        )[:10]
        top_net = sorted(
            ((node, self.net_flow.get(node, 0)) for node in offshore),
            key=lambda item: (-abs(item[1]), item[0]),
        )[:10]
        top_directed = sorted(
            self.shuttle_demand.items(), key=lambda item: (-item[1], item[0])
        )[:15]
        top_bidirectional = sorted(
            self.bidirectional_flow.items(), key=lambda item: (-item[1], item[0])
        )[:15]
        return {
            "nodes": len(self.nodes),
            "directed_arcs": len(self.directed_demand),
            "shuttle_arcs": len(self.shuttle_demand),
            "fixed_airport_arcs": len(self.fixed_airport_demand),
            "total_directed_demand": sum(self.directed_demand.values()),
            "total_shuttle_demand": sum(self.shuttle_demand.values()),
            "total_land_flexible_demand": sum(self.land_outbound.values())
            + sum(self.land_inbound.values()),
            "top_out_flow": top_out,
            "top_in_flow": top_in,
            "top_absolute_net_flow": top_net,
            "top_directed_shuttle_arcs": [([*key], value) for key, value in top_directed],
            "top_bidirectional_pairs": [([*key], value) for key, value in top_bidirectional],
        }


def build_q2_directed_flow_graph(data: ProblemData) -> Q2DirectedFlowGraph:
    airports = set(data.config.airports)
    directed: dict[tuple[str, str], int] = {}
    shuttle: dict[tuple[str, str], int] = {}
    fixed: dict[tuple[str, str], int] = {}
    land_outbound: dict[str, int] = {}
    land_inbound: dict[str, int] = {}
    in_flow = {node: 0 for node in (*data.config.airports, *data.config.facilities)}
    out_flow = dict(in_flow)
    affinity: dict[tuple[str, str], int] = {}
    for (origin, destination), pool in data.q2_pools.items():
        quantity = pool.quantity
        if origin == "LAND":
            land_outbound[destination] = land_outbound.get(destination, 0) + quantity
            continue
        if destination == "LAND":
            land_inbound[origin] = land_inbound.get(origin, 0) + quantity
            continue
        directed[(origin, destination)] = quantity
        out_flow[origin] = out_flow.get(origin, 0) + quantity
        in_flow[destination] = in_flow.get(destination, 0) + quantity
        if origin in airports or destination in airports:
            fixed[(origin, destination)] = quantity
            airport = origin if origin in airports else destination
            facility = destination if origin in airports else origin
            affinity[(facility, airport)] = affinity.get((facility, airport), 0) + quantity
        else:
            shuttle[(origin, destination)] = quantity
    net = {node: out_flow.get(node, 0) - in_flow.get(node, 0) for node in in_flow}
    bidirectional: dict[tuple[str, str], int] = {}
    facilities = tuple(data.config.facilities)
    for left_index, left in enumerate(facilities):
        for right in facilities[left_index + 1 :]:
            value = min(shuttle.get((left, right), 0), shuttle.get((right, left), 0))
            if value:
                bidirectional[(left, right)] = value
    return Q2DirectedFlowGraph(
        nodes=tuple((*data.config.airports, *data.config.facilities)),
        directed_demand=directed,
        shuttle_demand=shuttle,
        fixed_airport_demand=fixed,
        land_outbound=land_outbound,
        land_inbound=land_inbound,
        in_flow=in_flow,
        out_flow=out_flow,
        net_flow=net,
        bidirectional_flow=bidirectional,
        airport_affinity=affinity,
    )


def _route_facilities(route: RoutePlan, data: ProblemData) -> set[str]:
    facilities = set(route.service_facilities)
    for assignment in route.assignments:
        if assignment.origin_id in data.config.facilities:
            facilities.add(assignment.origin_id)
        if assignment.destination_id in data.config.facilities:
            facilities.add(assignment.destination_id)
    return facilities


def q2_sequence_features(
    sequence: tuple[str, ...],
    data: ProblemData,
    graph: Q2DirectedFlowGraph,
) -> Q2SequenceFeatures:
    route_distances = []
    for base in data.config.airports:
        distance = data.matrix[base][sequence[0]]
        distance += sum(data.matrix[left][right] for left, right in zip(sequence, sequence[1:]))
        distance += data.matrix[sequence[-1]][base]
        route_distances.append(distance)
    route_distance = min(route_distances)
    separate_distance = sum(
        min(2.0 * data.matrix[base][node] for base in data.config.airports)
        for node in sequence
    )
    detour_ratio = route_distance / separate_distance if separate_distance else 1.0
    directed_flow = sum(
        graph.shuttle_demand.get((left, right), 0)
        for left, right in zip(sequence, sequence[1:])
    )
    reverse_flow = sum(
        graph.shuttle_demand.get((right, left), 0)
        for left, right in zip(sequence, sequence[1:])
    )
    bidirectional = sum(
        min(
            graph.shuttle_demand.get((left, right), 0),
            graph.shuttle_demand.get((right, left), 0),
        )
        for left, right in zip(sequence, sequence[1:])
    )
    outbound = sum(
        graph.land_outbound.get(node, 0)
        + sum(
            graph.fixed_airport_demand.get((airport, node), 0)
            for airport in data.config.airports
        )
        for node in sequence
    )
    inbound = sum(
        graph.land_inbound.get(node, 0)
        + sum(
            graph.fixed_airport_demand.get((node, airport), 0)
            for airport in data.config.airports
        )
        for node in sequence
    )
    complementarity = min(outbound, inbound)
    prefix_outbound = 0
    seat_reuse = 0
    for node in sequence:
        prefix_outbound += graph.land_outbound.get(node, 0) + sum(
            graph.fixed_airport_demand.get((airport, node), 0)
            for airport in data.config.airports
        )
        seat_reuse += min(
            prefix_outbound,
            graph.land_inbound.get(node, 0)
            + sum(
                graph.fixed_airport_demand.get((node, airport), 0)
                for airport in data.config.airports
            ),
        )
    fixed_affinity = max(
        (
            sum(graph.airport_affinity.get((node, airport), 0) for node in sequence)
            for airport in data.config.airports
        ),
        default=0,
    )
    flexible = sum(
        graph.land_outbound.get(node, 0) + graph.land_inbound.get(node, 0)
        for node in sequence
    )
    expected_flow = outbound + inbound + directed_flow
    capacity_fit = 1.0 - min((expected_flow % seats) / seats for seats in (12, 16, 19))
    refuel_count = sum(node in data.config.refuel_facilities for node in sequence)
    longest_leg = max(
        min(data.matrix[base][sequence[0]] for base in data.config.airports),
        *(data.matrix[left][right] for left, right in zip(sequence, sequence[1:])),
        min(data.matrix[sequence[-1]][base] for base in data.config.airports),
    )
    complexity = longest_leg / 100.0 - 0.25 * refuel_count
    score = (
        8.0 * directed_flow
        + 2.0 * reverse_flow
        + 2.0 * bidirectional
        + 1.5 * complementarity
        + 1.0 * seat_reuse
        + 0.75 * fixed_affinity
        + 0.35 * flexible
        + 10.0 * capacity_fit
        + 4.0 * refuel_count
        - 0.06 * route_distance
        - 25.0 * detour_ratio
        - 3.0 * complexity
    )
    return Q2SequenceFeatures(
        sequence=sequence,
        route_distance_km=round(route_distance, 6),
        detour_ratio=round(detour_ratio, 6),
        directed_shuttle_flow=directed_flow,
        reverse_shuttle_flow=reverse_flow,
        bidirectional_flow=bidirectional,
        outbound_flow=outbound,
        inbound_flow=inbound,
        flow_complementarity=complementarity,
        seat_reuse_proxy=seat_reuse,
        fixed_airport_affinity=fixed_affinity,
        land_flexible_flow=flexible,
        capacity_fit=round(capacity_fit, 6),
        refuel_facilities=refuel_count,
        technical_stop_complexity_proxy=round(complexity, 6),
        score=round(score, 6),
    )


def flow_aware_local_sequences(
    data: ProblemData,
    routes: Sequence[RoutePlan],
    graph: Q2DirectedFlowGraph,
    *,
    max_sequence_length: int,
    budget: int,
) -> tuple[tuple[tuple[str, ...], ...], dict[tuple[str, ...], Q2SequenceFeatures]]:
    facilities = sorted({node for route in routes for node in _route_facilities(route, data)})
    required = {
        tuple(route.service_facilities)
        for route in routes
        if route.service_facilities
        and len(set(route.service_facilities)) == len(route.service_facilities)
    }
    base = set(
        candidate_service_sequences(
            data,
            seed_routes=routes,
            nearest_neighbors=0,
            high_demand_nodes=0,
        )
    )
    features: dict[tuple[str, ...], Q2SequenceFeatures] = {}
    generated: list[tuple[str, ...]] = []
    for length in range(2, min(max_sequence_length, len(facilities)) + 1):
        for sequence in permutations(facilities, length):
            feature = q2_sequence_features(sequence, data, graph)
            features[sequence] = feature
            generated.append(sequence)
    ranked = sorted(
        set(generated) - required - base,
        key=lambda item: (-features[item].score, len(item), item),
    )
    incumbent_sequences = required | base
    room = max(0, budget - len(incumbent_sequences))
    chosen = incumbent_sequences | set(ranked[:room])
    for sequence in chosen:
        features.setdefault(sequence, q2_sequence_features(sequence, data, graph))
    return (
        tuple(sorted(chosen, key=lambda item: (len(item), item))),
        {sequence: features[sequence] for sequence in chosen},
    )
