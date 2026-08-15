# Q2 Optimization Plan

## Objective

Q2 minimizes total aircraft usage time for all 4,000 outbound, inbound and
shuttle passengers. Every person remains on one flight, fixed airports are
honoured, LAND is airport-flexible, deliveries happen before pickups at the
same stop, each route returns to its base, and fuel/refuel/technical-stop rules
remain those of the shared solver core.

The initial algorithm was a restricted candidate-route master. The final
validated solver now uses that representation inside an exact-repair ALNS:

1. aggregate 4,000 people into 264 OD groups;
2. select a fixed four-route neighborhood with adaptive-roulette operators;
3. create bounded geometry-ranked one-to-five-service-node sequences, including
   targeted four-stop candidates;
4. enumerate airport, aircraft and cached technical-stop variants;
5. solve an integer route-multiplicity and passenger-allocation local master;
6. use SA for equal-primary structural transitions and exact elite-difference
   recombination between diverse local optima;
7. decompose aggregate interval loads into physical flights;
8. export individual assignments and run the independent Validator.

Any reported dual bound or MIP gap applies only to this finite restricted
master. It is not a global Q2 optimality certificate.

## Staged roadmap

### Q2-1 — main integration and restricted-master baseline

- Port only effective Q2 data, IO, candidate and master logic onto main.
- Keep `LegPhysics`, `SolverCache`, technical-stop search, evaluator, models
  and Validator authoritative from main.
- Freeze and replay the 19,736-minute canonical solution.
- Require a fresh 195/105-second solve to reach no worse than 19,736 before
  primary-budget or Q1-seed experiments proceed.
- Preserve 19,736 as best whenever that gate fails.

### Q2-2 — Hybrid LNS with exact repair

Only after Q2-1 approval: destroy small route/demand neighbourhoods and use a
small exact master to repack outbound, shuttle and inbound capacity, prioritising
whole-route ejection over ordinary single-batch relocation.

Status: **ADOPTED**. Exact local repair plus four destroy operators improved
the validated control from 19,736 to 19,482 in the first five-seed gate.

### Q2-3 — flow-aware 3–5 stop candidates

Use directed OD flow, seat reuse, LAND flexibility, capacity and fuel features
to rank bounded 3–5 stop candidates under a fixed generation/evaluation budget.

Status: bounded geometry-ranked long columns **ADOPTED**; the current
flow-aware scoring formula **REJECTED** by equal-budget A/B. The directed graph
remains a reusable information layer.

### Q2-4 — heuristic column enrichment

Iteratively add candidates guided by LP marginals or incumbent difficulty.
This is heuristic column generation, not Branch-and-Price and not a global
optimality proof.

Status: **REJECTED**. Three bounded enrich/re-solve rounds tied static quality
and cost more runtime. Standard adaptive-roulette ALNS was instead adopted and
reached 18,906.

### Q2-5 — optional learned ranking

Only after stable search logs exist, compare interpretable heuristic ranking
with logistic/tree ranking under identical candidate and evaluator budgets.

Status: **LOGGING COMPLETE; ML NOT READY**. Three context-feature runs contain
52,814 candidate rows, but 49,390 are censored and only 45 selected candidates
contributed primary improvement. There are too few independent runs for a
grouped train/validation/held-out experiment, so no model was trained.

### Q2-6 — advanced classical finalization

Status: **COMPLETE**. Fixed four-route neighborhoods, targeted four-stop
candidates, SA and elite recombination were adopted. Adaptive destroy size,
ejection-chain priority, local candidate cache, UCB1, default context ranking,
repeated visits, heuristic column generation and Branch-and-Price were not
promoted. The five-seed final benchmark reached best/median/worst
17,958/18,043/18,102; `outputs/q2/best` serves 4,000/4,000 with Validator PASS.

### Q2-7 — Round-2 final intensification and learning-data foundation

Status: **COMPLETE**. Extended control first refreshed 17,958 to 17,853/17,854.
Quality-constrained elite recombination, global-best restart and cross-exchange
were adopted; targeted 5-route remained a low-frequency intensifier. Current
fix-and-optimize, Local Branching feasibility, diversity-heavy partners and
default geometry+context portfolio were rejected. The final extended control
continuation produced a 4→3 route elimination and reached 17,595 minutes / 96
flights, 4,000/4,000, Validator PASS.

The learning-data foundation now contains 136,597 candidate events from six
run-grouped experiments, including 12,467 exact-evaluated rows, 12,343 true
negatives, 72 positives and 124,130 explicitly censored rows. Schema/split
infrastructure is complete, but candidate-ranking ML remains **NOT_READY**
because useful positives are concentrated in incumbent sequences. No model was
trained. See `ML_READINESS.md` and `NEXT_STAGE_RECOMMENDATION.md`.

## Universal promotion gates

- 4,000/4,000 people and zero Validator issues;
- internal and Validator metrics agree;
- primary objective never regresses;
- Q1 15,371 regression remains intact;
- candidate pool, seed, runtime and bound scope are reproducible;
- every `best` directory is an atomic copy of one validated run;
- stochastic methods report best/median/worst across multiple seeds.

## Q2-1 stop condition

Q2-1 stops after integration, regression, canonical replay and the fresh-solve
gate. If fresh solving is worse than 19,736, later budget and seed experiments
remain skipped until the solver-environment/incumbent issue is resolved.
