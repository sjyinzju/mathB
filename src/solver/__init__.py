"""Shared route construction and Q1 solver interfaces."""

from .baseline import solve_q1_baseline
from .cache import SolverCache
from .candidate_ranking import (
    ClusterCandidateRanker,
    RawDistanceRanker,
    RelatednessModel,
    load_fuel_signatures,
)
from .clustering import ClusterResult, average_linkage, cluster_sweep, pam_k_medoids
from .data import load_problem_data
from .evaluator import evaluate_route
from .exporter import export_q1_solution, load_q1_solution
from .improve import improve_q1_batch_relocation, improve_q1_route_ejection, improve_q1_savings
from .models import SolverConfig
from .physics import LegPhysics
from .technical_stops import augment_service_sequence

__all__ = [
    "augment_service_sequence",
    "average_linkage",
    "ClusterCandidateRanker",
    "ClusterResult",
    "cluster_sweep",
    "evaluate_route",
    "export_q1_solution",
    "improve_q1_batch_relocation",
    "improve_q1_route_ejection",
    "improve_q1_savings",
    "LegPhysics",
    "load_fuel_signatures",
    "load_q1_solution",
    "load_problem_data",
    "pam_k_medoids",
    "RawDistanceRanker",
    "RelatednessModel",
    "SolverCache",
    "SolverConfig",
    "solve_q1_baseline",
]
