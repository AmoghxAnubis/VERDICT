# Quality breakdown and availability gates

`confidence-v1` assesses a retained aggregation report using its exact scoring
evidence and dependency registry. It computes an uncalibrated diagnostic
`quality_score_bps` from 0 to 10,000 and records every availability gate. This is
not a probability of truth, a statistical confidence interval, or a usable feed.

Registry v1 does not contain independently supported domain attestations.
Consequently the independent-domain gate is always **unknown**, including for
synthetic graphs with all unknown nodes removed. Every v1 assessment is
`unavailable`, with `value=null`, `confidence=null`,
`independent_domain_count=null`, and `publication_ready=false`. A high diagnostic
quality score cannot override this. Supporting a successful independence gate
requires a versioned evidence contract and evaluator, not a policy bypass.

## Run and inspect

First migrate storage and retain an aggregation report using
[the aggregation workflow](AGGREGATION.md). Then pass its aggregation report ID:

```powershell
uv run --no-sync verdict db upgrade
uv run --no-sync verdict confidence <aggregation_report_id> --dataset replay:six-provider
```

The command prints and saves an immutable assessment. Repeated identical inputs
and policy return the same report ID. The default dataset is `live`; replay must
be selected explicitly. No network calls or current-time reads enter computation.

| Read-only route | Behavior |
| --- | --- |
| `GET /v1/confidence?dataset=replay:six-provider` | Saved assessments, newest cutoff first |
| `GET /v1/confidence/{report_id}?dataset=replay:six-provider` | Quality components, contributions, economic exposures, and gates |

History defaults to 10 reports and accepts `limit=1..100`. Reads never calculate
or save assessments. `/v1/feed/ETH-USD` remains unavailable and does not serve a
diagnostic candidate as a current price.

## Quality formula

Only sources with positive **final** aggregation weight contribute to quality.
All six sources remain in the report, including zero-weight exclusions through
their pinned aggregation. For total allocated weight `W`, each weighted mean is
`floor(sum(weight_i * component_i) / W)`. Zero total weight gives zero components,
zero quality, null candidate, and unknown concentration/dispersion measurements.

| Component | Definition |
| --- | --- |
| Uptime | Final-weighted mean of scoring-window fresh-data availability |
| Freshness | Final-weighted mean of the final tick's freshness score |
| Consistency | Final-weighted mean of scoring-window peer consistency |
| Comparison coverage | Final-weighted mean of scoring-window comparison coverage |
| Allocation | Actual allocated budget `W`, without scaling it up to 10,000 |
| Agreement | Linear penalty for maximum contributing-price deviation from the candidate |
| Resolved dependency share | 10,000 minus the conservatively rounded share of weight with any unresolved upstream |

The quality score is the **minimum** of these seven components. It cannot improve
by averaging away a weak component or by renormalizing a small allocation.
Positive weight with missing consistency or price evidence fails validation.

Let `d` be the largest exact fractional deviation `abs(price-candidate)/candidate`
among contributors, and `D=full_disagreement_bps`, initially 100. Agreement is
`max(0, 10000 - ceil(d * 100000000 / D))`. A 1% maximum deviation therefore yields
zero agreement under the default policy. `dispersion_bps=ceil(d*10000)` is retained
separately. The calculation uses exact rational arithmetic over Decimal prices,
so caller precision and rounding cannot alter a report. Zero-weight prices do not
enter dispersion, including retained outliers.

These are development rules, not calibrated market tolerances. Tight price
agreement and a coordinated majority can score highly without being true or
independent. Component scores are historical diagnostic measurements rather than
independent statistical trials. Existing statistical and historical policies are
unchanged.

## Availability gates

Every gate records its observed value, required threshold, comparison, pass/fail,
and reason. Equality passes at the threshold. Missing evidence yields `unknown`
and fails; it is never converted into a passing zero. Concentration shares use
integer **ceiling** rounding to avoid understating exposure.

| Gate | Default |
| --- | --- |
| Candidate | A non-null aggregation candidate exists |
| Eligible sources | At least 3 positive-weight providers |
| Allocated budget | At least 5,000 bps |
| Quality | At least 7,500 bps |
| Source concentration | At most 40% of allocated weight in any provider |
| Economic concentration | At most 50% in any shared economic dependency |
| Unknown share | No positive weight with unresolved upstreams |
| Registry not future | Review time is no later than cutoff |
| Registry not expired | Review age is at most 7 days at cutoff |
| Independent domains | At least 3 evidenced domains; unknown under registry v1 |

The allocation component also limits quality: with the default quality threshold,
a 5,000-bps allocation passes its budget gate but still fails quality. This is
visible in the breakdown. Provider count is kept distinct from domain count.

Economic memberships follow the weighting engine's transitive price, conversion,
and protocol paths. Shared transport and settlement are not economic groups.
Unknown upstreams on **any** path enter the unknown share. A source is counted once
in that pool even if it has multiple unresolved dependencies. Overlapping economic
groups are listed individually; Chainlink and Uniswap share the Chainlink protocol
in the bundled graph. Registry age checks use exact microsecond boundaries.

The bundled registry permits no allocation under its all-source unknown cap, so
its assessment has zero quality. Even a synthetic complete graph with high quality
fails independence. Deployment authenticity, fresh evidence review, held-out
calibration, and a supported domain-evidence format remain requirements before
usable feed output can be considered.

## Policies and retained evidence

`--policy path/to/policy.json` accepts the `ConfidencePolicy` shape. Export defaults:

```powershell
uv run --no-sync python -c "from verdict.confidence import ConfidencePolicy; print(ConfidencePolicy().model_dump_json(indent=2))"
```

Policies use strict bounded integers, require at least three providers/domains,
and cannot disable the independence gate. Threshold variants create distinct
assessment IDs without mutating prior reports or weighting histories.

Migration `0010` adds separate immutable confidence-round storage. Saving a new assessment requires
the aggregation, score, weights, registry, and original selected observations to
remain retained and valid. It reproduces the assessment and revalidates the full
aggregation evidence, even if that aggregation was saved earlier. New evidence
failures roll back the assessment. Concurrent duplicates save once. Reopened
reads validate report/policy hashes, arithmetic, gate decisions, and row metadata.
Database administrator modifications remain outside application-level immutability.
