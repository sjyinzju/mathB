# Q2 Optimization Plan

## Objective

Q2 minimizes total aircraft usage time for all 4,000 outbound, inbound and
shuttle passengers. Every person remains on one flight, fixed airports are
honoured, LAND is airport-flexible, deliveries happen before pickups at the
same stop, each route returns to its base, and fuel/refuel/technical-stop rules
remain those of the shared solver core.

The current algorithm is a restricted candidate-route master, not the ALNS
described by the older design note:

1. aggregate 4,000 people into 264 OD groups;
2. create deterministic one/two-service-node sequences from demand, geometry
   and a Q1 route seed;
3. enumerate airport, aircraft and cached technical-stop variants;
4. solve an integer route-multiplicity and passenger-allocation master;
5. decompose aggregate interval loads into physical flights;
6. export individual assignments and run the independent Validator.

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

### Q2-3 — flow-aware 3–5 stop candidates

Use directed OD flow, seat reuse, LAND flexibility, capacity and fuel features
to rank bounded 3–5 stop candidates under a fixed generation/evaluation budget.

### Q2-4 — heuristic column enrichment

Iteratively add candidates guided by LP marginals or incumbent difficulty.
This is heuristic column generation, not Branch-and-Price and not a global
optimality proof.

### Q2-5 — optional learned ranking

Only after stable search logs exist, compare interpretable heuristic ranking
with logistic/tree ranking under identical candidate and evaluator budgets.

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

