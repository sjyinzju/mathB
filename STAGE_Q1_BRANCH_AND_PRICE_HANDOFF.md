# Stage Q1 Branch-and-Price Handoff

## Status

`GLOBAL_OPTIMALITY_STATUS = BRANCH_AND_PRICE_INCOMPLETE`。
`FAST_PRICING_STATUS = CERTIFIED_EXACT`。

Current rigorous interval is:

`14091 <= OPT_Q1 <= 14730`

The unrounded global LP/tree lower bound is `14090.32748538012`; validated UB remains frozen 14,730. Gap is `4.3426512%`.

## Completed gates

- 1,041-column normal primal check: best 14,730, VALID.
- 1,041-column `<=14729`: proven infeasible, 449,347 nodes, 27,847,090 LP iterations, 2,771.92 s.
- Branch-aware exact pricing: canonical dual derivation, multiple ancestors, repeated traversals and future columns implemented.
- Branch tests: 13/13; combined pricing tests 22/22.
- N1-L and N1-R: both fully priced.
- Recursive best-bound queue/checkpoint: operational.
- N2-LL: fully priced and branched to depth 3.
- Elastic Phase-I/Farkas pricing path implemented for future restricted-RMP infeasibility.
- **Fast exact pricing oracle (P0)**: layered label-setting DP with 3-D dominance (cost/fuel/clock), subadditive reward bound, generation-time bound pruning and batch O(m log m) Pareto filter. Certified EXACT: 16/16 unit tests, 400-trial Pareto property test, Gate 8.2 real-duals cross-check 36/36 vs archived MILP (aggregate 3.0x wall-clock). MILP oracle remains permanent reference and runtime fallback. See `Q1_FAST_PRICING_REPORT.md`. Full suite 97/97. Commit `0bf403a`.
- **Checkpoint resumed** with the fast oracle: 8 additional nodes fully priced (N3-LLL, N3-LLR, N4-LLRL, N4-LLRR, N2-LR, N3-LRL, N4-LRLL, N4-LRLR), pricing 401–1,359 s/node.

## Resume point

Primary checkpoint:

`outputs/q1/exact/branch-and-price/20260816-recursive-best-bound/checkpoint.json`

It now contains 1,052 registry columns, 9 processed node records and 13 open nodes with complete histories (search continues in background, checkpointing per node). Resume command:

`python scripts/21_run_q1_recursive_branch_price.py --max-new-nodes <N>`

The next best-bound choice is determined from checkpoint; no manual reconstruction is required. Script mkdir calls are idempotent (`exist_ok=True`) so a killed resume never crashes again.

## Bottleneck and next action

Pricing is no longer the rate limiter (fast oracle 3.0x aggregate, ~768 s/node average). The measured blocker is **branching quality**: arc branching on the most-fractional aggregate arc gives a degenerate left/right pattern (+1.041 / 0.000 minutes bound gain, average ~0.82 min/node) while bound fathom needs ~640 minutes. The tree therefore grows at +1 open node per processed node and the global LB stalls at 14,090.327. This is exactly the handoff §15 scenario; the next action is **P2 Valid Inequality / Cut Audit**: analyse fractional root/node solutions (why LP reaches 14,090 while integer must be near 14,730), then design valid inequalities for this aggregated allocation formulation whose pricing coefficients can be handled exactly by the DP oracle. Only cuts passing the P2-C gate enter Branch-Price-and-Cut.

Do not create `Q1_GLOBAL_OPTIMALITY_CERTIFICATE.md` unless all open nodes are rigorously closed and the integer bound condition holds.
