# Historical exchange evaluation

The frozen [suite](../benchmarks/historical-v1.json) evaluates statistics-v2 on
retained Coinbase, Kraken, and Binance.US ETH/USD trade history. It uses one
calibration window and three later held-out windows, with disjoint warmup history.
Window names describe candidate regimes; the report also records measured returns
and price ranges rather than treating those names as observed facts.

The suite was fixed before the full capture and evaluation. It retains the existing
30-second freshness bound, six-MAD multiplier, 100-basis-point deviation floor,
three-source minimum, and 300-second history. Only the observation capacity bound
is raised to 100,000 for dense historical trade streams. No safety threshold is
tuned from these results. Research targets are 80% baseline availability, at most
5% unlabelled cross-source exclusions, and at least 95% conditional spike detection.
These are development acceptance targets, not security guarantees.

## Reproduce offline

```powershell
uv run --no-sync verdict history evaluate benchmarks/historical-v1.json benchmarks/captures --output .cache/historical-report.json
# Also fail the command if any held-out target is unmet or unscored:
uv run --no-sync verdict history evaluate benchmarks/historical-v1.json benchmarks/captures --output .cache/historical-report.json --require-targets
```

Evaluation opens neither a database nor a network connection. It can take several
minutes on the complete suite. The [retained result](../benchmarks/results/historical-v1.json)
contains capture checksums, suite/policy/code hashes, per-round IDs and exclusions,
per-source metrics, measured market movement, and explicit target outcomes. Changing
the engine, evaluator, inputs, or suite changes the report identity. Hashes establish
reproducibility, not provider signatures or independent ground truth.

## Capture another frozen suite

```powershell
uv run --no-sync verdict history fetch benchmarks/historical-v1.json .cache/new-historical-captures
```

Existing matching captures are validated and retained. A failed run can resume;
it does not overwrite completed archives. Requests have per-source rate delays,
bounded retries, and a 100-request budget per source/window. Incomplete ranges,
conflicting provider IDs, checksums, and stalled pagination fail explicitly.
Responses are retained as JSON text in compressed archives, alongside request
parameters, fetch time, endpoint, and body checksums. No credentials are required.

- Coinbase uses [public ETH-USD trades](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-trades).
  Numeric trade IDs seed a bounded historical search; the captured interval must
  then have contiguous IDs, monotonic event times, both time boundaries, and a
  consistent [pagination cursor chain](https://docs.cdp.coinbase.com/exchange/rest-api/pagination).
  A service changing those assumptions causes capture to fail.
- Kraken uses [historical time and sales through Trades](https://support.kraken.com/articles/360029077772-python-code-to-retrieve-historical-time-and-sales-trading-history-).
  Fractional epoch values retain lexical precision. Cursor progress and a final
  trade at or after the requested end establish the retrieved interval boundary.
- Binance.US uses [ETHUSD aggregate trades](https://docs.binance.us/), retaining
  the original aggregate record and its trade range. Aggregate IDs have a distinct
  `agg:` observation identity; they are not misrepresented as individual trades.
  Pagination advances by aggregate ID, including when a millisecond has more than
  1,000 aggregates. Empty successful historical ranges remain empty.

## Timing and interventions

The archives do not include historical first-receipt times. Every baseline assigns
an explicit assumed receipt at event time plus 250 milliseconds. Download time is
retained separately. This is an idealized timing study, not a reconstruction of
past network latency or the production polling schedule. Missing intervals are not
interpolated or filled with stablecoin markets. Oracle/DEX sources remain missing.

Each window includes 330 seconds of warmup and is evaluated every five seconds.
The evaluator applies the same event-time AND receipt-time cutoff, deterministic
tie ordering, and snapshot history boundary as ordinary analysis. TWAP still stops
at freshness expiry. Late receipts are invisible until their assumed arrival.

Paired runs compare the untouched historical baseline against five explicit
interventions, in separate replay namespaces:

| Intervention | Events affected after window start |
| --- | --- |
| Single-source spike | Coinbase +35%, seconds 30 through 90 |
| Coordinated market move | All three exchanges +10%, seconds 30 through 90 |
| Correlated shift | Coinbase and Kraken +35%, seconds 30 through 90 |
| Source outage | Drop Coinbase events, seconds 30 through 120 |
| Late receipt | Delay Kraken receipts by another 45 seconds, seconds 30 through 90 |

Intervals include their start and exclude their end. Original captures remain
unchanged. Modified observations retain the original payload plus intervention
metadata; they cannot enter live storage through this command.

Spike detection is scored only where the clean baseline is analyzed and accepts
the target, and its selected event belongs to the injected interval. No eligible
opportunities yields `not_scored`, never a perfect detection rate. Detection delay
is measured from the nominal injection start on the five-second round grid. It is
conditional on the timing assumption and sample; it is not an operational latency
guarantee. Natural exclusions are labelled **unlabelled**, not false positives.

Coordinated moves can produce transient disagreement when venues update at
different times. Correlated shifts measure median displacement and show the limits
of agreement under a bad majority. The median is still diagnostic; every result
keeps `publication_ready=false` and the feed remains unavailable.

## Recorded results: 2026-10-19

The retained run contains **21,000** normalized historical records and **1,440**
diagnostic rounds (240 baseline rounds plus five paired interventions).

| Window | Split | Coinbase / Kraken / Binance.US records, including warmup | Baseline availability | Injected spike detections / eligible opportunities |
| --- | --- | --- | --- | --- |
| 2023-01-08 12:00–12:05 UTC | Calibration | 1,688 / 20 / 0 | 0% | Not scored |
| 2023-03-12 08:00–08:05 UTC | Holdout | 911 / 173 / 284 | 81.7% | 9 / 9 |
| 2023-04-16 12:00–12:05 UTC | Holdout | 916 / 66 / 433 | 86.7% | 5 / 5 |
| 2024-08-05 06:20–06:25 UTC | Holdout | 15,222 / 1,287 / 0 | 0% | Not scored |

March and April have zero natural `extreme_deviation` exclusions among eligible
cross-source comparisons. This is not an independently labelled false-positive
rate. Their conditional injected-spike detection rate is 100% over only 14 eligible
opportunities, with first detection five seconds after nominal injection start.
It does not establish a population detection rate or production latency.

The coordinated +10% step creates three additional source exclusions in March
and two in April while sources update asynchronously. Two-source +35% shifts move
the diagnostic median by as much as 3,500 basis points. This exposes availability
and majority-agreement limitations instead of demonstrating manipulation resistance.

The August interval contains an approximately 3.3% decline and 5.0% intrawindow
range on Coinbase/Kraken, but no Binance.US ETHUSD aggregate records. January also
has no Binance.US records. Neither gets a fabricated third source, median, or
attack score. An empty API result is recorded as a data-availability gap; it does
not alone prove why the venue supplied no historical records.

`heldout_targets_met=false`, `six_provider_validation_complete=false`, and
`publication_ready=false`. The January calibration window could not support a
three-source calibration; **no thresholds were calibrated or relaxed**. The
historical-validation roadmap item remains open. Report identity:
`eb57d1f70383ea54760447773844b1adeb584c09600de4eb9c9c36b900b407c2`.

## Remaining validation

These four short windows cannot establish broad market accuracy, calibrated trust
or confidence, source independence, production latency, or six-provider readiness.
Historical Chainlink/Pyth/Uniswap evidence, longer predeclared regimes, genuine
receipt-time archives, independently labelled incidents, and an untouched later
holdout after any policy change remain necessary. The roadmap's historical
validation gate stays open when held-out targets are unmet or unscored.
