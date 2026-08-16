# Q1 Reduced-Cost Derivation

Primary LP 为

\[
\min \sum_{r\in R'} c_r x_r,
\quad \sum_{r\in R'} a_{gr}x_r=d_g\ \forall g,
\quad x_r\ge0.
\]

其中 `R'` 是当前 RMP columns，`c_r` 是官方 aircraft minutes，`a_gr` 是该 sortie 携带的 group `g=(origin,destination)` 人数。Demand equality 的自由 dual 为 `π_g`。Dual 为

\[
\max \sum_g d_g\pi_g,
\quad \sum_g a_{gr}\pi_g\le c_r\ \forall r\in R'.
\]

因此任意完整空间 column 的 reduced cost 严格为

\[
\bar c_r=c_r-\sum_g\pi_g a_{gr}.
\]

没有额外 aircraft/base global rows；base eligibility、capacity、fuel、landing 与 refuel legality 属于 column definition。旧 LP 的 per-column upper bound 已移除，否则 bound marginals 会污染标准 pricing 解释。

Pricing 按 3 bases × 3 aircraft types 分解。每个子问题在全部合法 ordered physical paths、refuel flags 与 allocations 中全局最小化上式。Route-time 项由 start arc、inter-position arcs、return arc、10 分钟普通 dwell 与 refuel额外 10 分钟组成；dual reward 仅来自该 base 的 fixed OD 与全部 LAND OD。九个子问题全部 `min rc >= -1e-7` 时，RMP dual 对完整 `R` 可行，故其 objective 是 full-space LP lower bound。

`PRICING_TOL=1e-7`。`-1e-13` 量级视为浮点噪声；真实新增列最小曾达到 `-18.4671052632`，远大于 tolerance。
