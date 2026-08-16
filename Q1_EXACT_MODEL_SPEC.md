# Q1 Exact Primary Model Specification

证明对象仅为 Q1 的 minimum total aircraft use time。Passenger time、flights、fuel 与 utilization 不参与 primary global certificate。

完整 column universe `R` 是所有单架次合法 allocated sorties。每列包含：3 个 base 之一、3 个 aircraft type 之一、1–5 个 ordered offshore landings、每次 landing 的 refuel flag、被服务的聚合 OD counts、官方分钟成本与 demand coverage。路线从 base 出发并回到同一 base；中间节点只能是 52 个海上设施。

固定机场 OD 只能从该机场出发；LAND OD 可选任意 base。总 allocation 至少 1 且不超过 aircraft seats。所有乘客在 base 上机，在 destination 第一次出现时下机，故 load 单调不增。空架次成本为正且 coverage 为零，删除不影响最优性。

物理合法性复用 `LegPhysics`、官方逐 leg `ceil(60d/v)`、逐 landing 10/20 分钟、tank/reserve、8 个 refuel facilities 与独立 Validator。Position-indexed exact pricing 明确包含所有 52 个节点、全部 1–5 landing lengths、全部合法 refuel decisions、technical stops，以及非相邻乃至相邻 repeated visits；没有 distinct-service、nearest-K、beam、Top-K 或 heuristic candidate cutoff。

Repeated visit 没有被 dominance 删除。Pricing 的 position variables 可在不同 positions 选择同一 facility；visited OR 只防止同一 destination 的 dual reward重复计入，不禁止物理重复。最终 38 个新增列虽未选出 repeated optimum，oracle 的 universe 仍完整包含它们。

Passenger allocation 是 integer bounded unit-weight selection：每个 eligible OD group 的 count 在 `[0,d_g]`，总 count 不超过 seats，只有访问 destination 才可取。该子结构直接作为整数变量进入 exact MILP，不使用 greedy heuristic。

Master 为 set partitioning：每个聚合 OD demand equality 必须精确满足，列变量非负。LP 中不使用旧实现的 `floor(d/count)` fractional caps；这些上界对整数解安全但会错误收紧 LP。

当前 exact mode 的安全 reduction 仅有 canonical OD-count ordering、完整 semantic route identity 和完全相同列去重。没有未证明的 route dominance。
