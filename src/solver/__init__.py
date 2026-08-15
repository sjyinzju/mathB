"""Shared route construction and Q1 solver interfaces."""

from .baseline import solve_q1_baseline
from .cache import SolverCache
from .data import load_problem_data
from .evaluator import evaluate_route
from .exporter import export_q1_solution, load_q1_solution, load_q2_solution
from .improve import improve_q1_batch_relocation, improve_q1_route_ejection, improve_q1_savings
from .models import SolverConfig
from .physics import LegPhysics
from .technical_stops import augment_service_sequence
from .q2 import (
    Q2MasterConfig,
    Q2RouteVariant,
    build_q2_variant,
    build_q2_variant_pool,
    build_separate_q2_baseline,
    candidate_pool_hash,
    candidate_service_sequences,
    q2_direction,
    solve_q2_master,
)
from .q2_lns import (
    DESTROY_OPERATORS,
    Q2LnsConfig,
    Q2LnsResult,
    adaptive_q2_destroy_size,
    build_q2_local_data,
    exact_q2_local_repair,
    exact_q2_elite_recombination,
    geometry_local_sequences,
    heuristic_q2_enrichment_repair,
    rank_q2_local_sequences,
    q2_solution_diversity,
    select_q2_neighborhood,
    solve_q2_lns,
)
from .q2_artifacts import atomic_promote_q2_run
from .q2_round2 import (
    Q2EliteEntry,
    Q2ElitePool,
    elite_pair_diagnostics,
    q2_difference_path_relink,
    q2_local_branching_feasibility,
)
from .q2_learning import (
    build_q2_learning_dataset,
    classify_q2_candidate_event,
    flatten_q2_candidate_event,
    grouped_q2_splits,
)
from .q2_flow import (
    Q2DirectedFlowGraph,
    Q2SequenceFeatures,
    build_q2_directed_flow_graph,
    flow_aware_local_sequences,
    q2_sequence_features,
)

__all__ = [
    "augment_service_sequence",
    "evaluate_route",
    "export_q1_solution",
    "LegPhysics",
    "load_q1_solution",
    "load_q2_solution",
    "load_problem_data",
    "improve_q1_savings",
    "improve_q1_batch_relocation",
    "improve_q1_route_ejection",
    "SolverCache",
    "SolverConfig",
    "Q2MasterConfig",
    "Q2RouteVariant",
    "build_q2_variant",
    "build_q2_variant_pool",
    "build_separate_q2_baseline",
    "candidate_pool_hash",
    "candidate_service_sequences",
    "q2_direction",
    "solve_q2_master",
    "DESTROY_OPERATORS",
    "Q2LnsConfig",
    "Q2LnsResult",
    "adaptive_q2_destroy_size",
    "build_q2_local_data",
    "exact_q2_local_repair",
    "exact_q2_elite_recombination",
    "geometry_local_sequences",
    "heuristic_q2_enrichment_repair",
    "rank_q2_local_sequences",
    "q2_solution_diversity",
    "select_q2_neighborhood",
    "solve_q2_lns",
    "atomic_promote_q2_run",
    "Q2EliteEntry",
    "Q2ElitePool",
    "elite_pair_diagnostics",
    "q2_difference_path_relink",
    "q2_local_branching_feasibility",
    "build_q2_learning_dataset",
    "classify_q2_candidate_event",
    "flatten_q2_candidate_event",
    "grouped_q2_splits",
    "Q2DirectedFlowGraph",
    "Q2SequenceFeatures",
    "build_q2_directed_flow_graph",
    "flow_aware_local_sequences",
    "q2_sequence_features",
    "solve_q1_baseline",
]
