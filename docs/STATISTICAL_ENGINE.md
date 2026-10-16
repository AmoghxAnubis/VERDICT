# Statistical analysis policy: statistics-v1

This implementation produces diagnostic rounds. It does not produce a VERDICT
consensus price, trust score, probability of correctness, or permission to publish.
`publication_ready` is always false; the feed endpoint remains unavailable.

## Input boundary and repeatability

A round fixes dataset, ETH-USD asset, timezone-aware UTC cutoff, source roster,
history window, freshness/clock policy, and algorithm version. Defaults are three
sources (Coinbase, Kraken, Binance.US), 300 seconds of history, 30 seconds of
freshness, five seconds of permitted future clock skew, and 10,000 observations.
The CLI/API use configured freshness and clock bounds.

The storage snapshot is selected in one SQL statement. Event timestamps AND first
receipt timestamps must be at or before the cutoff. A historical round never sees
a late-arriving observation before its recorded receipt. The query includes each
source's latest eligible observation and history since window start minus the
freshness interval, so a preceding price can seed TWAP only while it remains fresh.
Exceeding the observation limit fails explicitly; results are never silently truncated.

Source/event-time/observation-ID ordering resolves ties deterministically. Decimal
arithmetic uses local precision 60. Policy and report hashes use sorted-key JSON;
round identities include cutoff, policy, selected input IDs, and measurements.
CLI analysis saves an immutable report; API analysis is read-only. Live is the
default dataset for both observations and round history.

## Eligibility and cross-source diagnostics

Missing sources, stale latest observations, and event clocks beyond the allowed
receipt-time skew are excluded with explicit reasons. Freshness is
`exp(-age_seconds / freshness_seconds)`, while the hard age bound controls eligibility.

At least three fresh providers are required before computing a diagnostic median:

```text
reference = median(latest eligible source values)
MAD = median(abs(source_value - reference))
threshold = max(6 * MAD, reference * 100 / 10000)
deviation_bps = abs(source_value - reference) / reference * 10000
```

A difference strictly greater than the threshold yields `extreme_deviation`. The
100-basis-point floor handles zero MAD and small tick differences. These are
development parameters, not statistically calibrated security guarantees.
After exclusion, fewer than three inliers yields `insufficient_data`. With fewer
than three fresh providers, median/MAD remain null and otherwise fresh sources
receive `insufficient_sources`. A statistical median is not the feed's final value.

Provider count is not independence. Dependency weighting, reputation, persistent
quarantine/recovery, weighted consensus, and confidence are subsequent work.
Coordinated bad data and differing update cadences can defeat naive agreement;
historical stress calibration and source-specific timing policies remain necessary.

## Time-series measurements

TWAP is piecewise constant in event time. Each price covers only until the next
event or its freshness expiry, whichever comes first. Gaps contribute no duration.
The report exposes covered seconds and the fraction of the requested window.
TWAP averages covered time only; it is null if no positive-duration coverage exists.

Velocity is `(latest / previous - 1) * 10000 / elapsed_seconds` for the most recent
distinct-time pair when the gap is at most the freshness interval. Absolute velocity
over 200 basis points per second adds `high_price_velocity`; it does not by itself
exclude a source. All markets can legitimately move together.

`rms_return_bps` is the square root of the mean squared adjacent simple returns in
basis points over usable intervals. It is a sampled movement metric, not an
annualized or time-normalized volatility estimate. Single-event and gapped histories
do not invent return/velocity measurements. Equal timestamps collapse by the same
deterministic tie-break as latest selection.

## Fixture acceptance criteria

| Fixture | Cutoff UTC on 2026-10-16 | Required outcome |
| --- | --- | --- |
| market-baseline | 00:00:10 | Three inliers, analyzed |
| single-outlier | 00:00:10 | +35% source excluded; insufficient remaining sources |
| market-jump | 00:00:10 | All rise 10%; velocity signals, no cross-source exclusions |
| provider-outage | 00:00:40 | Old Binance.US trade stale; insufficient sources |

Additional automated checks cover zero MAD with a small tick difference, delayed
receipt, future event time, no data, stale TWAP gaps, deterministic input ordering,
decimal-context independence, bounded history, round immutability, API validation,
and replay isolation. These fixtures establish behavior, not real-world accuracy.

## Provider evidence and operating limits

Adapters pin direct USD markets using the official
[Coinbase ticker](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker),
[Kraken recent trades](https://docs.kraken.com/api-reference/market-data/get-recent-trades),
and [Binance.US recent trades](https://docs.binance.us/#get-recent-trades) interfaces.
REST samples the most recent trade; it is not a complete trade history or an
executable bid/ask quote. Streaming/backfill is still pending. Binance.US may be
thinly traded: a successful fetch can still return stale evidence.

Connector health counts attempts, successful receipts, inserted events, errors,
and consecutive failures. Successful duplicate polls increment receipts without
changing original observation timestamps or evidence. Health and successful inserts
commit together. Errors retain only their type and HTTP status, not credentials or
provider response bodies. Replay cannot modify live connector counters.
