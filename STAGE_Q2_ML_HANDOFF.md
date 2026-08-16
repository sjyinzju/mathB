# Stage Q2 Final ML Handoff

Q2 is frozen at 17,076 aircraft minutes, 94 flights, 4,000/4,000 served and
Validator PASS. Branch: `codex/q2-final-ml-optimization`; base frozen tag:
`q2-round3-17218` at `4a381e30732477d374f6dc68267f8793ee6607e6`.

The adopted method is a Logistic Regression second-stage ranker over geometry-screened
candidates, with two geometry safeguards and one deterministic exploration slot.
Only candidate order/exact-evaluation allocation changes; Shared Solver Core, exact
local MILP, feasibility and metrics remain authoritative.

Important reproducibility artifacts:

- offline audit/models: `outputs/q2/final-ml/offline`
- 48-run A/B results: `outputs/q2/final-ml/Q2_ML_ONLINE_AB.csv`
- immutable classical control: `outputs/q2/final-ml/frozen-control-17218`
- immutable final: `outputs/q2/final-ml/final-17076`
- official promotion: `outputs/q2/best`
- 94-flight copy/audit: `outputs/q2/best-94-flight`, `outputs/q2/final-ml/audit-94`

Do not reopen Q2 optimization absent contradictory validation evidence. Bounds in
all LNS logs are restricted-local-master bounds only. The raw 564 MB candidate log
and transient run/checkpoint logs intentionally remain untracked.
