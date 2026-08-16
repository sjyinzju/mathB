# Q1 Fast Exact Pricing Oracle — Implementation & Certification Report

**Date**: 2026-08-16
**Branch**: `codex/q1-exact-certification`
**Module**: `src/solver/q1_fast_pricing.py` (838 lines, pure-Python + NumPy label-setting DP)
**Driver integration**: `src/solver/q1_branch_price.py` (fast oracle primary, HiGHS MILP permanent reference/fallback)

---

## 1. Mandate

Replace the HiGHS MILP pricing oracle (99.9963% of B&P wall time: 3423.3 s pricing
vs 0.12 s RMP at the root) with a **fast oracle that is provably EXACT**. Hard
constraints from the handoff:

- No beam search, no top-K truncation, no heuristic pruning, no approximate dominance.
- Every dominance rule must carry a written proof and a regression test.
- Must support repeated facility visits, exact fuel/refuel physics, and arbitrary
  ancestor branch rows.
- The MILP oracle is **never removed**: it remains the permanent reference oracle
  and the runtime fallback.

## 2. Algorithm overview

Layered label-setting dynamic program over the route state space:

- **Layer** = number of sea landings so far (≤ 5), so each layer holds a fixed
  landing budget; termination (return-to-base) is considered at every label.
- **State** = `(node_index, visited_signature_tuple, fuel, cost, clock)`.
- **Frontier key** = `(node_index, visited_signature_tuple)` per depth; labels with
  the same key are filtered by exact dominance (§3.2), all others are kept.
- Expansion enumerates every feasible next leg (service or landing, with exact
  fuel checks and refuel decisions); branch rows enter as non-negative traversal
  costs (§3.4), so the state space and transitions are identical to the MILP
  column universe.
- The winner per subproblem is materialized through `evaluate_route` and
  re-verified with `branch_column_reduced_cost`; disagreement > 5e-6 raises,
  which triggers the MILP fallback in the driver.

## 3. Exactness arguments

### 3.1 Enumeration completeness

Labels are never dropped except by (i) exact dominance (§3.2), (ii) the reward
upper bound (§3.3) proven valid for every completion, or (iii) infeasible leg
physics identical to the evaluator. Repeated visits are allowed; the visited
signature tracks per-destination served-unit multisets, so no unit is ever
double-counted (`test_repeat_visits_do_not_double_count_units`). Signature
truncation beyond per-destination unit counts is proven lossless
(`test_signature_truncation_is_lossless`).

### 3.2 Three-dimensional dominance (cost, fuel, clock)

Within one frontier key `(depth, node, visited_signature)`, label `a` dominates
label `b` iff

```
cost_a <= cost_b  AND  fuel_a >= fuel_b  AND  clock_a <= clock_b
```

**Proof sketch.** Any completion sequence of legs available to `b` from this
state is also available to `a`: identical node and visited signature give
identical remaining reward opportunities; `fuel_a >= fuel_b` makes every leg and
refuel pattern feasible for `b` also feasible for `a`; `clock_a <= clock_b` keeps
all clocks no larger (Q1 has no duration constraint, and smaller clock never
hurts). The objective contribution of any shared completion is
`multiplier * (clock + duration_tail) - reward - branch_terms`: with
`multiplier >= 0`, `clock_a <= clock_b` and `cost_a <= cost_b` (which already
includes accumulated branch traversal costs), `a`'s completed reduced cost is
<= `b`'s. Hence removing `b` cannot remove the subproblem minimum.

**Why clock is required (tie-break safety).** With only `(cost, fuel)`,
two labels with equal cost and fuel but different clocks would dominate each
other; keeping the later-clock one can change the duration tie-break of the
master. The clock dimension makes dominance antisymmetric on the tie-breaking
quantity. Covered by `test_dominance_pruning_engages_and_stays_exact`.

**Why the visited signature is part of the key (counterexample).** Labels with
the same node/fuel/cost/clock but **different** visited signatures are NOT
comparable: remaining reward depends on which per-destination units were already
served. Concretely, destination `F` with two remaining units worth 9 and 9:
label `a` (has served none) can still earn 18 there, label `b` (has served one)
at most 9. Dominating `a` by `b` on `(cost, fuel, clock)` alone would delete a
strictly better continuation. The frontier therefore never merges across
signatures.

### 3.3 Subadditive reward bound (valid upper bound on future reward)

For a label with accumulated positive-signature sum `sig_sum` and `k` remaining
sea landings, the reward of **any** completion satisfies

```
reward(final) <= min( reward_max,
                      sig_sum + prefix_best[min(k, D)] )
```

where `prefix_best` is the prefix-sum table of the per-destination positive
`topseats` totals sorted descending, and `reward_max` is the global maximum
allocation reward. **Proof.** Reward is computed per destination as a concave
(topseats-saturating) function of the units served there; the incremental value
of serving any additional units at one destination is bounded by that
destination's remaining positive topseats sum. Serving at most `k` destinations
in the remaining landings, the most future reward achievable is bounded by the
sum of the largest `k` per-destination positive topseats totals
(subadditivity of the per-destination positive mass over the destination
multiset), plus what is already locked in (`sig_sum`). Note the earlier naive
"global top-K unit merge" bound is **invalid** (counterexample: `K=1`,
`seats=2`, one destination with two 9-value units vs global best single unit 10:
true optimum 18 > bound 10); the per-destination formulation is what makes the
bound valid.

Combined with `leg_min >= 0` (all leg costs and branch traversal costs are
non-negative, §3.4), any label with

```
cost + leg_min - reward_bound >= prune_rc      (materialized regime)
bound > prune_rc + TIE_TOL                     (seed-only regime)
```

cannot produce a column improving on the incumbent `prune_rc`.
Covered by `test_bound_pruning_engages_and_stays_exact`.

**Seed-only regime.** When no incumbent has been materialized yet (only a seed
upper bound from registry columns), pruning uses the strict `> +TIE_TOL`
threshold so labels that exactly tie the seed survive: if the seed equals the
true optimum, deleting all tying labels would wrongly return
`NoFeasibleColumn`. After any column materializes, the standard `>=` regime
restores. Covered by
`test_seeded_incumbent_equal_to_optimum_still_materializes_winner`.

### 3.4 Branch rows as non-negative traversal costs

For canonical branch rows `alpha * x <=/>= beta`, HiGHS minimization marginals
satisfy `lambda <= 0` (verified by `ArcBranchRow` unit tests), so the traversal
surcharge `-lambda * canonical_sign >= 0` for every leg. Hence:

- every leg cost in the DP is non-negative (`leg_min = 0` valid);
- any prefix-plus-return route is a valid column (Q1 has no duration limit),
  so termination is always available;
- the bound in §3.3 with `leg_min = 0` is conservative and valid under any
  ancestor branch history, which is precisely what child B&P nodes require.

### 3.5 Generation-time bound pruning (timing-only change)

The bound `cost + leg_min - reward_bound` depends **only** on `(cost,
signature, remaining_landings)` — not on fuel or clock. It is therefore safe to
evaluate it at candidate-generation time inside the expansion loop instead of at
the layer-end pruning pass. The set of surviving labels is identical; only the
timing of the same valid test changes. This is the single largest performance
lever (T2 label creation 12.9M -> 1.3M).

### 3.6 Batch Pareto filtering (`_pareto_filter`)

Layer-end dominance filtering is batched: labels are lexsorted by
`(cost asc, fuel desc, clock asc)` and scanned once while maintaining a strictly
decreasing 2-D frontier of `(fuel, clock)` with binary-search update, in
`O(m log m)`. Property-tested against an independent O(n^2) brute-force
non-dominated set on 400 randomized trials (including duplicate triples) —
exact agreement on all trials. Two earlier vectorized drafts were wrong
(prefix maxima mixing different labels; suffix/prefix query-direction bug) and
were caught by this property test before ever reaching certification data.

## 4. Certification evidence

### 4.1 Unit tests — `tests/test_q1_fast_pricing.py` (16 collected, all pass)

| Test | What it locks in |
|---|---|
| `test_fast_matches_brute_force_complete_universe` | exact match vs brute force over the complete column universe (parameterized over branch rows) |
| `test_fast_matches_milp_on_tiny_universe` | exact match vs HiGHS MILP oracle on a tiny instance |
| `test_randomized_duals_match_brute_force` | randomized dual vectors still match brute force |
| `test_dominance_pruning_engages_and_stays_exact` | dominance fires AND result stays exact |
| `test_bound_pruning_engages_and_stays_exact` | bound pruning fires AND result stays exact |
| `test_seeded_incumbent_equal_to_optimum_still_materializes_winner` | seed-only regime correctness |
| `test_repeat_visits_do_not_double_count_units` | repeated visits semantics |
| `test_signature_truncation_is_lossless` | signature compression losslessness |
| `test_no_eligible_demand_certifies_no_column` | correct NoFeasibleColumn certificate |
| `test_validation_mirrors_milp_oracle` | validator agreement with MILP oracle |
| `test_phase_one_multiplier_semantics` | PHASE_I `multiplier=0` semantics |

Full-suite regression: **97/97 tests pass** (81 pre-existing + 16 fast-pricing).

### 4.2 Gate 8.2 — real duals cross-check vs archived MILP oracle

Script `scripts/22_certify_fast_pricing.py`: 4 archived dual sources
(root-iter015, N1-L-iter001, N1-R-iter002, N2-LL-iter001) x 9 pricing
subproblems = **36 problems**, each compared against the previously archived,
validated HiGHS MILP pricing result (reduced cost and proven-optimal status).

**Result: 36/36 OK, failures = 0.** Every subproblem matched the archived MILP
reduced cost to `rc_tolerance = 1e-6` and reproduced the proven-optimal status.
Aggregate wall time: fast oracle **1798.6 s** vs MILP **5425.2 s** (**3.0x**
overall), per-problem speedups from **0.5x** (worst: root A01-T3) to **162.3x**
(best: N1-R A03-T1).

Artifact: `outputs/q1/exact/fast-pricing-certification/20260816-gate82-real-duals/summary.json`.

### 4.3 Performance (Gate run, unseeded, root-iter015 duals vs archived MILP)

| Subproblem | MILP (s) | Fast oracle v4 (s) | Speedup |
|---|---|---|---|
| A01-T1 | 84.3 | 8.3 | 10.1x |
| A01-T2 | 76.9 | 75.3 | 1.0x |
| A01-T3 | 82.5 | 155.7 | 0.5x |
| A02-T1 | 81.6 | 8.5 | 9.6x |
| A02-T2 | 78.9 | 75.4 | 1.0x |
| A02-T3 | 103.7 | 157.9 | 0.7x |
| A03-T1 | 61.8 | 1.5 | 41.4x |
| A03-T2 | 96.4 | 35.3 | 2.7x |
| A03-T3 | 106.0 | 78.2 | 1.4x |

Key lever: generation-time bound pruning (§3.5), e.g. A01-T3 label creation
drops to 3.5M with 18.3M bound-pruned candidates. The hard A01/A02-T3
subproblems sit below MILP parity at the root (no branch rows to tighten the
bound), but reach 1.0-3.7x at branch nodes where traversal costs raise the
bound's floor; aggregate certification run is 3.0x faster than MILP. The fast
oracle also removes the HiGHS solver bottleneck from the pricing ThreadPool
and degrades to the MILP oracle automatically on any exception.

## 5. Driver integration (`q1_branch_price.py`)

- Pricing submit path: `fast_exact_pricing(...)` primary for each
  `(base, aircraft_type)` subproblem inside the existing ThreadPoolExecutor.
- **Registry seeding**: before pricing, each subproblem receives
  `initial_incumbent_rc` = minimum reduced cost of any registry column of that
  `(base, type)` under the current duals. Every registry column satisfies the
  node's branch rows, so the seed is a valid upper bound on the subproblem
  minimum and can never remove an improving column.
- **Permanent MILP fallback**: any exception, time limit, or internal
  disagreement falls back to `exact_pricing` (HiGHS MILP) with a logged reason;
  correctness never depends on the fast oracle.

## 6. Certification status

```
FAST_PRICING_STATUS = CERTIFIED_EXACT
```

granted on 2026-08-16 after: 16/16 unit tests, 97/97 full-suite regression,
400-trial Pareto property test, and Gate 8.2 36/36 real-duals cross-check
against the archived validated MILP oracle. Only after this status was granted
did the Branch-and-Price checkpoint `20260816-recursive-best-bound` resume for
global integer closure.

## 7. Residual risks and safeguards

- The fast oracle's numeric path uses the same `evaluate_route` /
  `branch_column_reduced_cost` arithmetic as the MILP column evaluation, and
  every materialized winner is double-checked (tolerance 5e-6) before entering
  the RMP.
- Any future mismatch at runtime triggers automatic MILP fallback and a logged
  warning; the gate script can be re-run at any time (read-only against
  archived duals).
- T3 subproblems at MILP parity remain the pricing bottleneck if the tree
  deepens; options are stronger per-destination reward bounds or A* ordering,
  both correctness-neutral given the §3 proofs.
