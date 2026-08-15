# Q2 Optimization Handoff

Date: 2026-08-15  
Branch: `codex/q2-main-integration`  
Shared base: `main@eabfcfe`

## Current validated best

`outputs/q2/best` is the atomic copy of
`outputs/q2/runs/20260815-q2-alns-seed3`.

| Metric | Current best | Canonical control | Change |
|---|---:|---:|---:|
| Aircraft time | **18,906 min** | 19,736 min | **-830** |
| Passenger time | 266,308 min | 270,734 min | -4,426 |
| Flights | 103 | 107 | -4 |
| Fuel | 146,442.5 kg | 152,910.4 kg | -6,467.9 |
| Seat utilization | 0.8537035000 | 0.8182982554 | +0.0354052446 |
| Served / Validator | 4,000 / PASS | 4,000 / PASS | unchanged legality |

The historical 19,736 artifact remains immutable in
`outputs/q2/baseline-19736`. Every new best is materialized to individual
assignments, re-evaluated, reconciled with the independent Validator, and only
then atomically promoted.

## Completed stages and decisions

- **RMP calibration — REJECT as an optimization direction.** E1 300+0 and E2
  270+30 both reproduced 20,275 on the same 357 sequences, 3,116 variants and
  23,251 compatible assignments. Restricted bound 16,292 and gap 19.645%; the
  gap applies only to that finite restricted master.
- **Route ejection + exact local MILP repair — ADOPT.** Five seeds from 19,736
  produced best/median/worst 19,482/19,495/19,620. All four destroy operators
  contributed primary gains. Whole-route ejection occurred in three seeds and
  reduced the best flight count from 107 to 106.
- **Directed flow graph — ADOPT as shared information.** It has 55 nodes, 160
  non-LAND directed arcs, 56 shuttle arcs carrying 800 people, and 2,720 LAND-
  flexible passengers kept separately to avoid airport triple counting.
- **Bounded local 3–5-stop columns — ADOPT.** Geometry-ranked five-seed
  best/median/worst was 19,143/19,267/19,360. New 3-stop columns were repeatedly
  selected in accepted repairs; a 5-stop column was also selected in the flow
  experiment. No accepted 4-stop evidence was observed.
- **Flow-aware ranking — REJECT.** Under identical 24-sequence and 8-second
  local budgets, geometry reached 19,143 while flow reached 19,339. Graph data
  stays available, but its current scoring formula is not in the control.
- **Heuristic iterative column enrichment — REJECT.** Three rounds (8/16/24
  sequence prefixes, 2/2/4 seconds) tied static geometry at 19,143 but took
  178.6 seconds instead of 144.3 seconds. This was heuristic enrichment, not
  exact reduced-cost pricing.
- **Standard ALNS — ADOPT.** Classical adaptive roulette over the four proven
  destroy operators, exact repair, and strict-improvement acceptance gave
  five-seed best/median/worst 18,906/18,991/19,031 (population variance
  2,110.96). All seeds improved 19,143; no SA or Bandit was used.

## Current bottleneck and interfaces

The main cost is first-time technical-stop augmentation for new service
sequences. A run-scoped `SolverCache` is shared across all neighborhoods; the
ALNS best saw local candidate counts of 4–24 and successful local compatible-
assignment sizes of 90–1,170. Successful local masters were solved to local
gap zero, but these are only `restricted_local_master` certificates.

Key interfaces are `Q2LnsConfig` / `solve_q2_lns`,
`exact_q2_local_repair`, `build_q2_directed_flow_graph`, and
`flow_aware_local_sequences`. Physics, fuel, refuel and technical stops still
come exclusively from `LegPhysics`, `SolverCache` and the shared evaluator.

## Next recommendation

Freeze 18,906 as the current Q2 control. The logs cover more than 200 repairs,
but geometry candidates do not yet record every rejected candidate's full
feature vector, so the dataset is not a sound supervised ranking table. Add
complete candidate-level negative logging before any ML A/B. Branch-and-Price
is currently **REJECT**: bounded long columns already work, while iterative
enrichment showed no quality gain. Repeated visits and Bandit remain late
optional; Q3 has not been started.
