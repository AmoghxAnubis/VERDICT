# Full-pipeline synthetic benchmarks

The frozen `benchmarks/adversarial-v1.json` suite evaluates all six providers through
source scoring, stateful quarantine/recovery, dependency-constrained weights,
weighted-median aggregation, and quality/availability assessments. It does not
change the historical suite or any engine default.

## Reproduction

```powershell
uv run --no-sync verdict benchmark benchmarks/adversarial-v1.json --output .cache/adversarial-report.json
uv run --no-sync verdict benchmark benchmarks/adversarial-v1.json --output .cache/adversarial-report.json --require-targets
```

The strict command writes the complete reviewable report before exiting with code
1 when an attack target fails or cannot be scored. The current frozen run fails
the correlated-majority target. This failure is an expected measured limitation,
not a reason to weaken the target. The retained result is
`benchmarks/results/adversarial-v1.json`.

Execution is offline and opens no database. Inputs, policies, interventions,
registry snapshots and IDs, input checksums, pipeline report IDs, per-source
decisions, weights, quality and all availability gates are retained. Source and
fixture SHA-256 use UTF-8 text with normalized newlines for Windows/Linux checkout
compatibility. A changed fixture checksum is rejected. Rerunning with the same
evidence and code reproduces the report ID; changed source code changes that ID.

## Frozen design

Eight scenarios run against two registry profiles, yielding 400 complete diagnostic
rounds. Generated observations arrive every ten seconds from second zero; rounds
run from 60 through 300 seconds. Interventions run from 120 inclusive to 180
exclusive, followed by 120 seconds of recovery observation. Receipts normally
coincide with events; the delayed-data case adds 45 seconds. Selection must satisfy
both event and receipt cutoffs, including when late data becomes visible later.

The bundled profile preserves every unresolved upstream and therefore allocates
zero weight. The explicitly synthetic profile removes unknown nodes solely to
exercise positive weights. Known shared protocol/chain/conversion relationships
remain. Neither graph supplies independent-domain evidence. Oracle/DEX provenance
comes from synthetic templates, not authentic historical chain reads.

The suite deliberately uses short **test policies**: a 60-second scoring window,
three fresh/comparison/distinct-observation requirements, two new observations and
ten seconds for recovery, and a 2,000-basis-point increment/source budget.
All other defaults and full expanded policies are retained in the report. These
settings are not a validation or calibration of production-default timing.

## Measured synthetic-profile results

| Intervention | Maximum candidate shift against paired baseline | First quarantine | First positive weight after intervention ends |
| --- | ---: | ---: | ---: |
| Coinbase +35% spike | 1 bp | 0 seconds | 50 seconds |
| All providers +10% market move | 1,000 bp | None | No quarantine |
| Chainlink/Uniswap +35% correlated pair | 0 bp | 0 seconds | 50 seconds |
| Four-provider +35% correlated majority | 3,500 bp | Shifted providers remain accepted | Shifted providers remain accepted |
| Coinbase outage | 1 bp | 20 seconds | 50 seconds |
| Chainlink/Uniswap shared outage | 0 bp | 20 seconds | 50 seconds |
| Kraken receipts delayed 45 seconds | 0 bp | 20 seconds | 50 seconds |

Candidate shifts use conservative integer basis-point ceilings and only rounds
where both candidates exist. Missing-candidate opportunities are counted separately;
an unscored target does not pass. Quarantine delay is measured from intervention
start, not from expiry of the last fresh observation. Recovery means first positive
weight, not full restored allocation. Paired quarantine counts count decisions
across rounds, rather than distinct sources or statistically labelled errors.

The isolated and correlated-pair shifts satisfy the frozen five-basis-point target.
The correlated majority exceeds it by a wide margin and quarantines the two
unshifted providers instead. A coordinated market move creates no extra quarantine
decisions in this synthetic grid. These observations do not establish real-market
false-positive or detection rates.

All 400 assessments remain unavailable, with null usable value/confidence and
publication disabled. The synthetic healthy baseline reaches quality 9,700/10,000
and 9,996 allocated basis points while still failing independent-domain availability.

## Remaining gates

Default-policy and held-out six-provider calibration, independently supported
upstream/deployment evidence, real Redis/PostgreSQL behavior, sustained service
load, and live outage recovery remain separate work. This benchmark exercises the
pure diagnostic pipeline; existing storage/CLI/API contracts test persistence.
It does not enable the feed or establish coordinated-majority resistance.
