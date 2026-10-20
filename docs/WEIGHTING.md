# Quarantine and dependency-constrained weighting

`weighting-v1` advances persistent source states from retained diagnostic scores.
It applies immediate penalties, delayed recovery, and limits on final relative
weights. These are development allocations, not a consensus price, calibrated
confidence, or permission to publish. Every report keeps `confidence=null`,
`independent_domain_count=null`, and `publication_ready=false`.

## Run and inspect

First migrate storage, register dependency evidence, ingest or replay observations,
and save a scoring report as described in [source scoring](SOURCE_SCORING.md).
Apply that report once, using its dataset and printed report ID:

```powershell
uv run --no-sync verdict db upgrade
uv run --no-sync verdict weight <scoring_report_id> --dataset replay:six-provider
```

The command prints and saves the complete weighting report. Reapplying the same
score and weighting policy returns its original result, including after subsequent
rounds have advanced. It does not advance recovery or ramp weight again.

| Read-only route | Behavior |
| --- | --- |
| `GET /v1/weights?dataset=replay:six-provider` | Saved reports, newest cutoff first |
| `GET /v1/weights/{report_id}?dataset=replay:six-provider` | One report in its dataset |

The default dataset is `live`. History defaults to 10 reports and accepts
`limit=1..100`. API reads never apply transitions. Scores and weights have separate
histories; `/v1/sources` does not silently substitute a saved allocation for current
source health. The live feed continues to return HTTP 503.

## State transitions

Each source starts in `probation` with zero weight. The current scoring tick must
be fresh and an inlier, the score must be non-null, and cold-start requirements
must be satisfied before recovery evidence can accumulate.

| Rule | Default |
| --- | --- |
| Quarantine score threshold | Below 5,000 bps |
| Recovery score threshold | At least 7,500 bps |
| Recovery evidence | 10 new observations across consecutive applied rounds |
| Minimum recovery duration | 60 seconds from the first qualifying round |
| Maximum preliminary weight increase | 250 bps per round |
| Per-source quality budget | 2,500 bps |

Missing, stale, invalid-clock, outlier, or unscored current evidence removes weight
immediately and resets recovery. Loss of established history also quarantines the
source. Cold-start probation has zero weight. A gap between applied cutoffs other
than exactly one scoring step quarantines all sources for that round.

For probation or quarantined sources, qualifying evidence increments the recovery
counter only when the selected observation ID differs from the previous round's
ID. Repeated polling does not earn additional credit. Quarantined sources enter
`recovering`; probation retains its name until activation. Recovery requires both
the count and elapsed-time thresholds. Scores below the recovery threshold reset
the counter and start time. Inactive sources always receive zero weight.

On activation, preliminary weight is the smaller of 250 bps and
`floor(score_bps * source_budget_bps / 10000)`. Active sources use that quality
target, with upward changes limited to 250 bps per applied round. Decreases take
effect immediately. An active source may remain active between the quarantine
and recovery thresholds; this is the hysteresis band.

## Dependency limits

Caps apply to shares of the **final allocated total**, rather than percentages of
the original 10,000-bps budget:

| Cap | Default maximum final share |
| --- | --- |
| Each individual source | 40% |
| Each shared economic dependency | 50% |
| All sources with any unresolved upstream dependency, pooled together | 20% |

Economic exposure follows transitive `price`, `conversion`, and `protocol` edges.
The bundled graph therefore groups Chainlink and Uniswap under the Chainlink
protocol. Delivery and settlement edges are still visible in registry inspection;
they do not create economic caps. Unknown dependencies on any path place a source
in the conservative unknown pool, including unknowns behind transport or settlement.
This is not a claim that uncapped paths are independent or operationally safe.

If preliminary weights exceed 10,000 bps in total, they are scaled down with
integer floor rounding. For a cap with maximum share `c`, current group mass `G`,
and outside mass `O`, permitted group mass is `floor(O*c/(10000-c))`. An oversized
group is reduced proportionally with floor rounding. Caps are visited in canonical
ID order until all hold, revisiting overlaps after reductions. Each correction
strictly decreases integer mass; the starting total is at most 10,000 units.
Suppressed weight is never redistributed. Rounding can leave a smaller allocation
than an optimal solver would produce.

**Every source in the bundled registry has unresolved upstreams.** The unknown
pool therefore covers all six providers, and its 20% cap permits only zero total
weight. Even established, active sources cannot bypass this by normalizing small
weights to 100%. Reports expose `no_allocatable_weight` and all 10,000 bps as
unallocated. Nonzero test allocations use an explicitly synthetic completeness
assumption; that graph is not provider evidence.

## Persistence and policy

Migration `0008` adds immutable weighting reports and one serialized head per
dataset. Applying a score locks that dataset's head, validates retained evidence,
and commits the report and pointer together. Concurrent duplicates return one
result; competing successors cannot fork the chain. Cutoffs advance strictly.
A pointer that is missing, reset, or inconsistent with the newest retained report
causes an error, including on duplicate application. Failure rolls back all writes.
Database administrators remain outside application-level immutability guarantees.

Reports pin their previous report, score, registry, scoring policy, and complete
weighting policy. Reads validate checksums, source coverage, totals, share caps,
and storage metadata. The dataset's chain cannot change registry or either policy;
policy experiments need a separately populated replay dataset. There is no reset
command that silently discards quarantine history.

`--policy path/to/policy.json` accepts the `WeightingPolicy` shape. Export defaults:

```powershell
uv run --no-sync python -c "from verdict.weighting import WeightingPolicy; print(WeightingPolicy().model_dump_json(indent=2))"
```

Recovery must exceed the quarantine threshold. Policy values are strict bounded
integers; source and economic share limits cannot be 100%, and the unknown limit
may be zero. These thresholds have not been calibrated on held-out six-provider
history. Scores still use unweighted peer comparisons, which can follow a
coordinated majority. [Diagnostic weighted-median aggregation](AGGREGATION.md)
consumes these saved weights. [Quality and availability assessments](CONFIDENCE.md)
evaluate them separately. Independent-domain evidence, deployment matching, and
publication remain subsequent work.
