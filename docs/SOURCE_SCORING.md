# Diagnostic source scoring

`scoring-v1` measures observed availability, freshness, and agreement with other
providers over a fixed time grid. Scores are integer quality indicators from 0 to
10,000, not voting weights, probabilities of truth, calibrated confidence, or
authorization to publish. The defaults are development rules that have not been
calibrated against held-out six-provider history. The existing exchange-only
historical evaluation and `statistics-v2` remain unchanged.

## Run and inspect

```powershell
uv run --no-sync verdict db upgrade
uv run --no-sync verdict dependencies register
uv run --no-sync verdict replay simulations/six-provider.json
uv run --no-sync verdict score --registry-id e6c74460fbff40dd4517bba9b715e47094b39b0cce0dfda44b9df7146820618b --dataset replay:six-provider --at 2026-10-16T00:00:10Z
```

The short six-provider fixture demonstrates cold start, not established history.
The command saves an immutable report and prints its `report_id`, policies, source
breakdowns, and each sampled decision. Repeating identical inputs is idempotent.
For live observations, omit `--dataset` and optionally `--at`. Replay requires an
explicit cutoff. The selected registry must already be retained; no newest-version
fallback is used.

| Read-only route | Behavior |
| --- | --- |
| `GET /v1/scores?dataset=replay:six-provider` | Saved scoring reports, newest cutoff first |
| `GET /v1/scores/{report_id}?dataset=replay:six-provider` | One report, scoped to its dataset |

The default dataset is `live`. History defaults to 10 reports and accepts
`limit=1..100`. Reads do not compute new reports. Scoring reports are separate from
statistical `/v1/rounds`. `/v1/sources` continues to expose current observation and
connector state with `trust_score=null`; a historical score is not silently
presented as a current consensus weight.

## Fixed observation schedule

The default window is 600 seconds, sampled every 10 seconds. The cutoff is floored
to an epoch-aligned UTC grid without floating-point time conversion. For cutoff
`T`, samples are `T-590, T-580, ..., T`; the window's left boundary is excluded.
The report records the effective cutoff. Missing observations occupy sample slots;
an operator cannot improve uptime by saving fewer statistical rounds.

At each tick, select the greatest tuple of event time, provider sequence, and
observation ID per source, subject to **both** event time and original receipt time
being no later than that tick. Late receipts never rewrite earlier ticks. Older
arrivals cannot replace newer events. A newer invalid-clock event is reported as
unavailable rather than silently falling back to an older one.

Storage selects a pre-window anchor for each source plus every event becoming
visible during the window, including late receipts of older events. It fails above
the input limit instead of truncating history. Limits are 360 ticks and 100,000
candidate observations, with at most 2,160 selected input IDs (six per tick).

Each provider has explicit timing rules, initially 30 seconds maximum event age
and five seconds maximum event-time lead over receipt time. Equality is accepted.
Tick availability is `missing`, `clock_skew`, `stale`, or `fresh`; clock skew takes
precedence over staleness. Polling a repeated event does not refresh its receipt.

## Components and score

Let `N` be the scheduled tick count, `F` the fresh count, `C` the eligible peer
comparison count, and `I` the inlier count. Ratios use integer floor rounding.

| Component | Definition |
| --- | --- |
| `uptime_bps` | `floor(10000 * F / N)` |
| Tick freshness | `floor(10000 * exp(-age / freshness_limit))` when fresh; zero otherwise |
| `mean_freshness_bps` | Floor of the sum of tick freshness scores divided by `N` |
| `current_freshness_bps` | Freshness at the final tick |
| `consistency_bps` | `floor(10000 * I / C)`; null when `C=0` |
| `comparison_coverage_bps` | `floor(10000 * C / N)` |

"Uptime" means **observed fresh-data availability**. It cannot distinguish a
provider outage from missing collector data. HTTP success counts and current
connector-health counters are not historical scoring inputs. Chainlink's cadence
can exceed the initial freshness limit; the rule is not automatically relaxed.

Consistency excludes the target source and requires at least three other fresh
providers at the same tick. Compute their median `M`, median absolute deviation
`MAD`, and threshold `max(6 * MAD, M * 100 / 10000)`. The target is an outlier only
when its absolute deviation exceeds the threshold. Peer IDs, reference, threshold,
target value, and decision are retained. A source cannot count itself as evidence;
fewer than four fresh providers overall cannot yield a default comparison.

For a current fresh, comparable source, the uncapped score is the minimum of
uptime, mean freshness, current freshness, consistency, and comparison coverage.
A current outlier gets zero immediately. Current missing, stale, or invalid-clock
evidence gets zero and `unavailable`. A current fresh source with insufficient
peers has a null score and `unscored`, even if past comparisons were successful.
This distinguishes a measured failure from a comparison that cannot be made.

Decimal calculations use precision 60 and fixed rounding, independent of the
caller's Decimal context. Tick counts are time-grid measurements, not independent
statistical trials or labelled accuracy estimates.

## Cold start and policy selection

The source stays in cold start until its window contains at least 30 fresh
samples, 30 peer comparisons, and 10 distinct observations selected while fresh.
Each unmet requirement is explained separately. A comparable source in cold start
has its score capped at 2,500. Distinct IDs prevent one repeatedly polled trade from
maturing the source. Other component limits still apply: one good tick in a 60-tick
window can score at most 166.

The `cold_start` boolean is independent of current availability. `established`
means the history requirements are met; it is not a health classification and can
accompany zero. History can age out and a source can re-enter cold start.
[Persistent quarantine, slow recovery, and hysteresis](WEIGHTING.md) consume saved
scores in a separate weighting chain. This score alone must not be used to readmit
a source after an attack.

`--policy path/to/policy.json` accepts the `ScoringPolicy` JSON shape. Export the
defaults for an explicit review:

```powershell
uv run --no-sync python -c "from verdict.scoring import ScoringPolicy; print(ScoringPolicy().model_dump_json(indent=2))"
```

Scoring defaults are independent of legacy statistical settings such as
`VERDICT_FRESHNESS_SECONDS`. Customize `source_timing` explicitly for all six
providers to test another timing policy. The complete policy and hash are retained.
Incomplete timing maps, impossible cold-start counts, and oversized/nonintegral
grids fail validation. Parameter variants have distinct hashes; algorithm changes
require a new version. Choose targets and fresh holdouts before evaluating changes.

## Evidence and limits

Reports pin the registry hash and review time, complete scoring policy, selected
observation IDs, a checksum of their retained payloads, and every sampled decision.
Registry unknowns remain in source explanations. `registry_review_after_cutoff`
makes after-the-fact review explicit; a false value does not attest that this graph
was deployed or known at the historical cutoff. Deployment matching and evidence
validity policy remain pending.

Migration `0007` adds separate immutable scoring-round storage. Saving verifies
report/policy hashes and requires the registry to be retained and the exact selected
input payloads to exist in the report's dataset. Missing or changed evidence rolls back the insert.
Reopened reads validate hashes and storage metadata. Changed visible evidence,
policy, or registry creates a different report ID; prior reports stay intact.
Database administrators are outside application-level immutability guarantees.

Agreement is not independently known truth. Peer comparisons currently use
provider identities without dependency-adjusted weights. A coordinated majority
can appear consistent and exclude an honest minority. Asynchronous market updates
can produce transient exclusions. Tests record these limitations; they do not
establish calibrated accuracy. Registry unknowns remain visible in scores;
the separate weighting stage applies conservative dependency share caps.

All scoring reports retain `independent_domain_count=null`, `confidence=null`, and
`publication_ready=false`. Historical validation, upstream independence,
and consensus gates remain open. Quarantine/recovery and dependency-aware weights
are implemented separately and do not change these scoring guarantees.
The live feed continues to return HTTP 503.
