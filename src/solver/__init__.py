"""Shared route construction and Q1 solver interfaces."""

from .alns import ALNSRunResult, Q1ALNSConfig, improve_q1_alns
from .baseline import solve_q1_baseline
from .cache import SolverCache
from .data import load_problem_data
from .evaluator import evaluate_route
from .exporter import export_q1_solution, load_q1_solution
from .improve import improve_q1_batch_relocation, improve_q1_route_ejection, improve_q1_savings
from .models import SolverConfig
from .physics import LegPhysics
from .q1_or import (
    EliteRoutePool,
    Q1MasterConfig,
    Q1MasterResult,
    Q1RestrictedLPResult,
    collect_elite_route_pool,
    route_identity,
    solve_restricted_lp,
    solve_route_pool_master,
)
from .technical_stops import augment_service_sequence

__all__ = [
    "ALNSRunResult",
    "augment_service_sequence",
    "evaluate_route",
    "export_q1_solution",
    "EliteRoutePool",
    "LegPhysics",
    "load_q1_solution",
    "load_problem_data",
    "improve_q1_alns",
    "improve_q1_savings",
    "improve_q1_batch_relocation",
    "improve_q1_route_ejection",
    "Q1ALNSConfig",
    "Q1MasterConfig",
    "Q1MasterResult",
    "Q1RestrictedLPResult",
    "collect_elite_route_pool",
    "route_identity",
    "SolverCache",
    "SolverConfig",
    "solve_q1_baseline",
    "solve_restricted_lp",
    "solve_route_pool_master",
]
