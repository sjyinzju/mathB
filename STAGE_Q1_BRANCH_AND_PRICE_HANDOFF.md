# Stage Q1 Branch-and-Price Handoff

## Status

`GLOBAL_OPTIMALITY_STATUS = BRANCH_AND_PRICE_INCOMPLETE`。

Starting SHA was `b0dffe1fdd39184e3d627eaeaef7645d67b36a14`. Current rigorous interval is:

`14091 <= OPT_Q1 <= 14730`

The unrounded global LP/tree lower bound is `14090.327485380118`; validated UB remains frozen 14,730. Gap is `4.3426512%`.

## Completed gates

- 1,041-column normal primal check: best 14,730, VALID.
- 1,041-column `<=14729`: proven infeasible, 449,347 nodes, 27,847,090 LP iterations, 2,771.92 s.
- Branch-aware exact pricing: canonical dual derivation, multiple ancestors, repeated traversals and future columns implemented.
- Branch tests: 13/13; combined pricing tests 22/22; full suite 81/81.
- N1-L and N1-R: both fully priced.
- Recursive best-bound queue/checkpoint: operational.
- N2-LL: fully priced and branched to depth 3.
- Elastic Phase-I/Farkas pricing path implemented for future restricted-RMP infeasibility.

## Resume point

Primary checkpoint:

`outputs/q1/exact/branch-and-price/20260816-recursive-best-bound/checkpoint.json`

It contains 1,047 registry columns, processed N2-LL record and five open nodes with complete histories. Resume command:

`python scripts/21_run_q1_recursive_branch_price.py --max-new-nodes <N>`

The next best-bound choice is determined from checkpoint; no manual reconstruction is required.

## Bottleneck and next action

Branch-node RMP time = 0.12622 s; exact pricing time = 3,423.323801 s; pricing share = 99.9963%. N2-LL required 1,216.84 pricing seconds and did not raise global LB. Continuing the current oracle node-by-node is computationally inefficient.

The single highest-value next optimization is an exact DP/label-setting pricing oracle exploiting 3 bases、3 types、at most 5 offshore landings、seat capacity at most 19、monotone load and explicit fuel/refuel state. It must be cross-checked against the current position-indexed MILP on exhaustive tiny cases, randomized cases and real node duals. The MILP remains the correctness reference. After pricing throughput improves, resume this checkpoint and only then evaluate limited strong branching/pseudo-costs.

Do not create `Q1_GLOBAL_OPTIMALITY_CERTIFICATE.md` unless all open nodes are rigorously closed and the integer bound condition holds.
