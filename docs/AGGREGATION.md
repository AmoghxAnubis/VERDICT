# Diagnostic weighted-median aggregation

`aggregation-v1` computes a reproducible candidate from one retained weighting
report and its pinned scoring report. It does not use the latest database prices.
Separate [quality/availability assessments](CONFIDENCE.md) consume these reports;
aggregation reports retain `confidence=null`, `independent_domain_count=null`, and
`publication_ready=false`. A candidate is not a usable live feed result.

## Run and inspect

After migration, save scores and apply weighting as described in
[source scoring](SOURCE_SCORING.md) and [weighting](WEIGHTING.md). Pass the printed
weighting report ID, rather than a scoring report ID:

```powershell
uv run --no-sync verdict db upgrade
uv run --no-sync verdict aggregate <weighting_report_id> --dataset replay:six-provider
```

The command saves an immutable aggregation report and prints it. Repeating the
command returns the same report ID. The default dataset is `live`; a replay report
must be explicitly selected. No current-time cutoff or network request is needed.
Different saved weighting rounds can be aggregated in any order without advancing
or resetting their quarantine histories.

| Read-only route | Behavior |
| --- | --- |
| `GET /v1/aggregations?dataset=replay:six-provider` | Saved reports, newest cutoff first |
| `GET /v1/aggregations/{report_id}?dataset=replay:six-provider` | One immutable report in its dataset |

History defaults to 10 reports and accepts `limit=1..100`. API reads do not compute
or persist reports. `/v1/feed/ETH-USD` continues to return HTTP 503 with null value
and confidence; its existing reason remains `consensus_engine_not_implemented`
until independent-domain evidence and the full usable output contract are implemented.

## Selection and exact-half behavior

The inputs are all six sources from the scoring report's final tick, paired with
their weighting states. Report IDs, cutoff, dataset, registry, and scoring policy
must match. Each weighting entry must reference that tick's exact observation ID.
The report retains the complete samples and weighting entries, including values,
peer comparisons, recovery state, preliminary/final weights, and reasons.

A contributing observation must be fresh, a peer-comparison inlier, active, and
assigned positive final weight. Cold-start, unscored, or zero-quality sources
cannot contribute. Zero-weight entries remain in the explanation with exclusion
reasons. Inconsistent positive weight causes an error: aggregation does not discard
such an input and rescale the remainder, which could breach dependency share caps.

Order contributing inputs by numeric price, then source identity for equal prices.
Let `W` be their total final weight. The candidate is the first price whose
cumulative weight `C` satisfies `2*C >= W`. At an exact half-weight boundary, this
selects the lower price. For example, prices 100 and 200 with equal positive
weights select 100. No midpoint or floating-point calculation is used. Repeated
prices contribute their individual weights; tie ordering cannot change the price.

Only integer cumulative weights and Decimal comparisons enter the median. The
candidate's trailing fractional zeros are removed without Decimal arithmetic, so
equivalent prices and different caller precision/rounding settings reproduce the
same candidate encoding. All prices remain decimal strings in JSON, including
very small values that Decimal may serialize in exponent notation.

Positive total weight yields `status=diagnostic_candidate`. Zero total weight
yields `status=unavailable`, `candidate_value=null`, no ordered sources, and the
reason `zero_total_weight`. Both statuses explicitly report that confidence and
availability gates are pending in the aggregation stage; saved confidence
assessments evaluate those gates separately. No ordinary median, previous result, or latest
observation substitutes for missing weight. Source count is not an independently
verified domain count.

All sources in the bundled dependency registry have unresolved upstreams. Its
conservative unknown pool cap permits zero total weight, even after activation.
The short six-provider replay also starts in probation. Both therefore produce
unavailable aggregation reports. Nonzero tests use an explicitly synthetic
complete-dependency graph, which does not establish real provider independence.

## Retained evidence and validation

Reports pin the full aggregation policy, both upstream policies and reports,
registry, cutoff, dataset, and scoring input checksum. Migration `0009` adds
separate aggregation storage; it preserves observations, scores, weights, and heads.
The existing statistical rounds and frozen historical benchmark are unchanged.

Saving validates hashes, requires the referenced score/weight/registry evidence,
and reproduces the report from those retained inputs. All selected scoring
observation payloads must still match their checksum. Final tick selection is
checked against original source identity, value, event/receipt boundaries, and
source-specific freshness/skew rules. A failure rolls back the new report.
Concurrent duplicate saves retain one identical report. Reopened reads check
report arithmetic, exclusions, hashes, and storage metadata. Later observations
cannot change a saved candidate. Database administrator modification is outside
application-level immutability guarantees.

This slice establishes deterministic aggregation, not calibrated accuracy or
manipulation resistance. Scores still compare unweighted peers; a coordinated
majority can appear consistent. Confidence breakdowns and availability gate
assessments are implemented separately. Independently supported domain evidence,
replay benchmarks for the complete consensus pipeline, and publication remain later work.
