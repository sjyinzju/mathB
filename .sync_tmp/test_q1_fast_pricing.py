"""Certification tests for the fast exact pricing oracle.

The brute-force enumerator and the MILP reference oracle
(``q1_pricing.exact_pricing``) are the ground truths; the fast DP must
reproduce the exact minimum reduced cost, duration tie-break and allocation
pattern on the tiny universe under fixed, branch-perturbed and randomized
duals. Dedicated cases pin down the exactness of the two pruning rules
(dominance within identical (depth, node, visited set) and the bound
prune), the lossless top-seats signature truncation, and the repeat-visit
unit accounting regression.
"""
from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import pytest

from src.solver import load_problem_data
from src.solver.data import ProblemData
from src.solver.models import DemandPool
from src.solver.q1_pricing import ArcBranchRow, exact_pricing
from src.solver.q1_fast_pricing import fast_exact_pricing


_TINY_NODES = ("F001", "F006", "F010")
_TINY_DUALS = {
    ("A01", "F001"): 13.25,
    ("LAND", "F006"): 9.5,
    ("A01", "F010"): 7.75,
    ("A02", "F001"): 1000.0,
}


def _tbp():
    """The brute-force reference implementation from the pricing tests."""

    path = Path(__file__).resolve().parent / "test_q1_branch_pricing.py"
    spec = importlib.util.spec_from_file_location("tbp_reference", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tiny_data() -> ProblemData:
    return _tbp()._tiny_data()


def _assert_matches_brute_force(
    duals,
    branch_duals,
    *,
    base="A01",
    aircraft="T1",
    nodes=_TINY_NODES,
    max_landings=3,
    multiplier=1.0,
):
    tbp = _tbp()
    data = tbp._tiny_data()
    brute = tbp._brute_force_branch_price(
        data, duals, base, aircraft, nodes, max_landings, branch_duals,
        route_cost_multiplier=multiplier,
    )
    fast = fast_exact_pricing(
        data, duals, base, aircraft, candidate_nodes=nodes,
        max_landings=max_landings, branch_duals=branch_duals,
        route_cost_multiplier=multiplier,
    )
    assert fast.proven_optimal
    assert abs(fast.reduced_cost - brute[0]) <= 1.0e-6
    assert fast.route_duration_minutes == brute[1]
    assert tuple(fast.allocation_pattern) == tuple(brute[2])
    return fast


@pytest.mark.parametrize(
    "branch_duals",
    [
        {},
        {ArcBranchRow(("A01", "F001"), "<=", 1): -7.0},
        {ArcBranchRow(("A01", "F001"), ">=", 1): -7.0},
        {
            ArcBranchRow(("A01", "F001"), "<=", 1): -3.5,
            ArcBranchRow(("F001", "F006"), ">=", 1): -5.25,
        },
        {
            ArcBranchRow(("A01", "F001"), "<=", 1): -1.0,
            ArcBranchRow(("F001", "F006"), ">=", 1): -2.0,
            ArcBranchRow(("F006", "A01"), "<=", 1): -4.0,
        },
        {ArcBranchRow(("F006", "F006"), ">=", 2): -50.0},
    ],
)
def test_fast_matches_brute_force_complete_universe(branch_duals):
    _assert_matches_brute_force(_TINY_DUALS, branch_duals)


def test_fast_matches_milp_on_tiny_universe():
    data = _tiny_data()
    branch_duals = {
        ArcBranchRow(("A01", "F001"), "<=", 1): -1.0,
        ArcBranchRow(("F001", "F006"), ">=", 1): -2.0,
    }
    milp = exact_pricing(
        data, _TINY_DUALS, "A01", "T1", candidate_nodes=_TINY_NODES,
        max_landings=3, branch_duals=branch_duals,
    )
    fast = fast_exact_pricing(
        data, _TINY_DUALS, "A01", "T1", candidate_nodes=_TINY_NODES,
        max_landings=3, branch_duals=branch_duals,
    )
    assert milp.proven_optimal and fast.proven_optimal
    assert abs(milp.reduced_cost - fast.reduced_cost) <= 1.0e-6
    assert milp.route_duration_minutes == fast.route_duration_minutes


def test_randomized_duals_match_brute_force():
    rng = random.Random(20260816)
    keys = list(_TINY_DUALS)
    arcs = [
        ("A01", "F001"), ("F001", "F006"), ("F006", "A01"),
        ("F001", "F010"), ("A01", "F010"), ("F006", "F010"),
        ("F006", "F006"),
    ]
    for trial in range(15):
        duals = {key: rng.uniform(-20.0, 30.0) for key in keys}
        branch_duals = {}
        if trial % 2 == 0:
            branch_duals = {
                ArcBranchRow(
                    rng.choice(arcs), rng.choice(["<=", ">="]),
                    rng.randint(1, 3),
                ): -rng.uniform(0.0, 10.0)
            }
        _assert_matches_brute_force(
            duals, branch_duals, max_landings=rng.choice([2, 3, 4]),
        )


def test_dominance_pruning_engages_and_stays_exact():
    """Dominance must actually prune labels without changing the optimum."""

    fast, diag = fast_exact_pricing(
        _tiny_data(), _TINY_DUALS, "A01", "T1",
        candidate_nodes=_TINY_NODES, max_landings=3,
        return_diagnostics=True,
    )
    assert diag.dominance_prunes > 0
    _assert_matches_brute_force(_TINY_DUALS, {})


def test_bound_pruning_engages_and_stays_exact():
    """A strong incumbent activates the bound prune; optimum unchanged.

    With zero duals the global reward bound collapses, so deep labels are
    provably unable to beat the incumbent and must be bound-pruned while
    the certified minimum stays identical.
    """

    data = _tiny_data()
    duals = {key: 0.0 for key in _TINY_DUALS}
    baseline = fast_exact_pricing(
        data, duals, "A01", "T1",
        candidate_nodes=_TINY_NODES, max_landings=4,
    )
    seeded, diag = fast_exact_pricing(
        data, duals, "A01", "T1",
        candidate_nodes=_TINY_NODES, max_landings=4,
        initial_incumbent_rc=baseline.reduced_cost,
        return_diagnostics=True,
    )
    assert diag.bound_prunes > 0
    assert abs(seeded.reduced_cost - baseline.reduced_cost) <= 1.0e-9
    assert seeded.route_duration_minutes == baseline.route_duration_minutes
    assert seeded.allocation_pattern == baseline.allocation_pattern


def test_seeded_incumbent_equal_to_optimum_still_materializes_winner():
    data = _tiny_data()
    baseline = fast_exact_pricing(
        data, _TINY_DUALS, "A01", "T1",
        candidate_nodes=_TINY_NODES, max_landings=3,
    )
    seeded = fast_exact_pricing(
        data, _TINY_DUALS, "A01", "T1",
        candidate_nodes=_TINY_NODES, max_landings=3,
        initial_incumbent_rc=baseline.reduced_cost,
    )
    assert abs(seeded.reduced_cost - baseline.reduced_cost) <= 1.0e-9
    assert seeded.stops and seeded.allocation_pattern


def test_repeat_visits_do_not_double_count_units():
    """Regression: a self-loop reward branch makes revisits optimal, but
    the allocation reward of a visited destination must count once."""

    duals = dict(_TINY_DUALS)
    branch_duals = {ArcBranchRow(("F006", "F006"), ">=", 2): -100.0}
    fast = _assert_matches_brute_force(duals, branch_duals, max_landings=4)
    assert fast.repeated_visit
    # The 3 units of the LAND-F006 group count exactly once despite the
    # repeated landings (brute-force equality already pins the pattern).
    assert sum(
        count for origin, _, count in fast.allocation_pattern
        if origin in ("LAND", "A01")
    ) <= 12


def test_signature_truncation_is_lossless():
    """More units than seats at one destination: only the top seats matter,
    and the DP must agree with exhaustive enumeration."""

    full = load_problem_data()
    people = tuple(f"P{i}" for i in range(30))
    pools = {
        ("A01", "F001"): DemandPool("A01", "F001", people),
        ("LAND", "F006"): DemandPool("LAND", "F006", ("Q1", "Q2")),
    }
    data = ProblemData(full.config, full.matrix, pools)
    duals = {("A01", "F001"): 4.0, ("LAND", "F006"): 30.0}
    tbp = _tbp()
    brute = tbp._brute_force_branch_price(
        data, duals, "A01", "T1", ("F001", "F006"), 3, {},
    )
    fast = fast_exact_pricing(
        data, duals, "A01", "T1", candidate_nodes=("F001", "F006"),
        max_landings=3,
    )
    assert abs(fast.reduced_cost - brute[0]) <= 1.0e-6
    assert fast.route_duration_minutes == brute[1]
    assert tuple(fast.allocation_pattern) == tuple(brute[2])


def test_no_eligible_demand_certifies_no_column():
    data = _tiny_data()
    fast = fast_exact_pricing(
        data, _TINY_DUALS, "A03", "T1",
        candidate_nodes=_TINY_NODES, max_landings=3,
    )
    assert fast.status == "NoFeasibleColumn"
    assert fast.proven_optimal
    assert fast.certified_no_negative_column
    assert fast.reduced_cost is None
    assert fast.negative_column_found is False


def test_validation_mirrors_milp_oracle():
    data = _tiny_data()
    with pytest.raises(ValueError):
        fast_exact_pricing(
            data, _TINY_DUALS, "A99", "T1", candidate_nodes=_TINY_NODES,
        )
    with pytest.raises(ValueError):
        fast_exact_pricing(
            data, _TINY_DUALS, "A01", "T9", candidate_nodes=_TINY_NODES,
        )
    with pytest.raises(ValueError):
        fast_exact_pricing(
            data, _TINY_DUALS, "A01", "T1", candidate_nodes=("A01",),
        )
    with pytest.raises(ValueError):
        fast_exact_pricing(
            data, _TINY_DUALS, "A01", "T1",
            candidate_nodes=_TINY_NODES, max_landings=99,
        )
    with pytest.raises(ValueError):
        fast_exact_pricing(
            data, _TINY_DUALS, "A01", "T1",
            candidate_nodes=_TINY_NODES,
            branch_duals={ArcBranchRow(("A01", "F001"), "<=", 1): 5.0},
        )


def test_phase_one_multiplier_semantics():
    _assert_matches_brute_force(
        _TINY_DUALS,
        {ArcBranchRow(("A01", "F001"), "<=", 1): -7.0},
        multiplier=0.0,
    )
