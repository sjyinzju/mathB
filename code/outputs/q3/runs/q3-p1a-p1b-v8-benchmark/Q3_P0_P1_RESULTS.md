# Q3 P0–P1 Results

## Baseline

- Old Stage 1: 30180 min.
- Old Stage 2: 30180 min, temporary 158/160.

## P0 projection and feedback

- The valid Stage 2 schedule was projected by deleting optional assignments only.
- New Stage 1: 30180 min.
- Saved versus old Stage 1: 0 min.

## Final

- Stage 1: 30180 min, 171 flights, validator 0 violations.
- Stage 2: 30180 min, temporary 158/160, validator 0 violations.
- Certified Stage 1 gap against 14125 min: 53.197%.

## P1 components

- Multi-heuristic seed modes and Top-K interface are implemented.
- Hard-skeleton / flexibility / regret-priority mode is implemented.
- Same-day 2-to-1 multi-flight ruin-and-recreate is implemented.
- Time-aware offshore waiting scheduler and actual-timing export are implemented.

## Validation

All promoted CSV candidates were independently re-read and checked by the unchanged Q3 validator.
