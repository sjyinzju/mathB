# Q3 PRO V2 — V1 Baseline Revalidation

## Repository provenance

- Source branch: `codex/q3-pro`
- Source/final V1 artifact commit: `4e42a986b37fdaef45d7861402639a7321eabac5`
- V1 result-producing code commit: `643a4c784c277ab74122033eeb70b74177d4045e`
- V2 branch: `codex/q3-pro-v2`
- V2 initial commit: `4e42a986b37fdaef45d7861402639a7321eabac5`
- Revalidation run: `runs/v1-revalidation`

The V1 artifact commit is a descendant of the result-producing code commit. The
V1 worktree was clean when V2 was forked. The original Q1/Q2 worktrees and
`codex/q3-pro` were not modified.

## Independent CSV revalidation

The checked-in CSVs were loaded into the in-memory Q3 model, exported afresh,
and passed to the independent `validate_solution("q3", ...)` path. Metrics below
are from the new Validator reports, not copied from an old `metrics.json`.

### Stage 1

| Mandatory | Aircraft min | Passenger min | Flights | Fuel kg | Utilization | Validator |
|---:|---:|---:|---:|---:|---:|---|
| 3840/3840 | 29155 | 241018 | 165 | 231384.5 | 0.5702173595 | valid, 0 issues |

### Stage 2 at the strict Stage 1 cap

| Cap | Mandatory | Temporary | Aircraft min | Passenger min | Flights | Fuel kg | Utilization | Validator |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 29155 | 3840/3840 | 157/160 | 29155 | 250735 | 165 | 231384.5 | 0.5929281323 | valid, 0 issues |

Dynamic unserved temporary IDs: `P1102`, `P2239`, `P3290`.

The revalidation runner also compared every in-memory objective component with
the freshly exported Validator metrics and found no mismatch. Its fixed-flight
assignment MILP again proved 157/160 optimal only for the incumbent flight
structure; it is not an unrestricted certificate.

## Libraries and bounds entering V2

- Deduplicated route variants: 3116.
- Routes used by the V1 final Stage 1 solution: 143.
- Persistent independently validated V1 elite anchors: 3; the V1 run retained 9
  in memory.
- Persistent flight-column library: none. V1 pricing produced 2681 additional
  finite-pool columns only as diagnostics, so V2 must materialize and feed them
  back to primal search.
- Restricted LP before batch pricing: 16613.127192982458.
- Finite-pool LP after batch pricing: 15197.677631578947.
- Globally valid layered-flow lower bound: 14125 after integer ceiling.
- V1 certified Stage 1 gap: 51.552049%; no global optimality claim.

## Test status at the V2 fork

- Q3-focused suite: 23 passed.
- Full `code/tests`: 77 passed, 4 failed, 2 errors.
- The six non-passing cases are pre-existing and outside Q3: missing
  `code/data/q1-relatedness-consensus.csv`; stale Q1 expected best 15371 versus
  checked-in 15418 and a short-run improvement assertion; a Q2 CRLF byte-level
  round-trip mismatch; and non-atomic extra files in `outputs/q2/best`.
- No Q1/Q2 file was changed to mask these failures.

## Stale or historical artifacts

- The V1 final report records the result-producing commit but predates the final
  artifact commit; this provenance file supplies the relationship explicitly.
- Absolute paths embedded in the V1 historical report point at the V1 worktree.
  They remain historical and are not rewritten.
- V1 has a route library but no separately persisted `FlightColumnLibrary`.
- Restricted and finite-pool LP values are diagnostics and are not global lower
  bounds.

## Frozen V2 baseline

V2 therefore starts from the independently revalidated lexicographic incumbent:

```text
Stage 1: 29155 aircraft minutes, 165 flights, 3840/3840 mandatory
Stage 2: 157/160 temporary under cap 29155, 3840/3840 mandatory
```

Any V2 promotion must independently validate both CSV pairs and compare them by
the canonical lexicographic objective functions.
