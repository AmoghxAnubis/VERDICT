# Coinbase streaming and REST recovery

Continuous ingestion can subscribe to Coinbase Exchange's public ETH-USD
`matches` and `heartbeat` channels. Both subscriptions must be acknowledged before
bootstrap. `match` and initial `last_match` messages use the same trade identities
as REST observations, preserving decimal prices and original receipts across
transport overlap. Other providers continue to poll independently.

The implementation follows Coinbase's [channel documentation](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels):
matches can be dropped, and heartbeat trade IDs indicate when REST recovery is
needed. The broader exchange message sequence also covers non-trade events; it is
not treated as a contiguous trade sequence.

## Starting and restarting

Apply migration 0004 with `verdict db upgrade`, then run:

```text
verdict ingest --source coinbase --coinbase-stream
verdict ingest --coinbase-stream
```

The second command streams Coinbase and polls the other five providers. Set
`VERDICT_COINBASE_STREAM_ENABLED=true` for the same continuous behavior without a
CLI flag. REST polling remains the default, and one-shot ingestion remains REST.
The explicit streaming flag rejects `--once` and individual non-Coinbase sources.

First launch records the newest REST trade as `baseline_trade_id` and
`last_trade_id`. It does not backfill or claim completeness before this baseline,
even when earlier polling observations already exist. The subscription is open
before REST bootstrap so later matches are buffered by the WebSocket client.

After restart/reconnect, the worker loads its retained checkpoint and retrieves
the [product trades endpoint](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-trades).
It fixes a target at the newest trade in the first response, then follows opaque
`CB-AFTER` headers as `after` parameters to retrieve older pages, as specified by
Coinbase's [pagination rules](https://docs.cdp.coinbase.com/exchange/rest-api/pagination).
Newer trades appearing during pagination do not change that recovery target.

## Continuity and failure behavior

Trade IDs must cover every integer from checkpoint + 1 through the recovery
target. Repeated/overlapping pages are deduplicated with conflict checks. Missing
IDs, malformed responses, stale REST heads, stopped pagination, and page-budget
exhaustion fail explicitly. A triggering match must agree with its recovered REST
trade. This conservative continuity rule does not silently treat an unexplained
provider ID gap as complete history.

The full recovered range, health receipt/insert counts, and checkpoint advance
commit in one transaction. A conflicting observation or competing checkpoint
writer rolls the transaction back. Normal matches advance one trade at a time;
heartbeat or match gaps trigger REST recovery before progress is accepted. Late
and duplicate observations preserve first receipt and original evidence.

Recovery is bounded by `VERDICT_COINBASE_RECOVERY_PAGE_SIZE` (default 1000, maximum
1000) and `VERDICT_COINBASE_RECOVERY_MAX_PAGES` (default 20, maximum 100). Persistent
gaps beyond that budget leave the checkpoint intact and retry; operators must
investigate coverage or increase the configured bound. There is no automatic
checkpoint reset or silent jump to the latest trade.

Malformed messages, provider errors, disconnects, and missing subscription or
heartbeat messages cause reconnect with capped exponential backoff. REST and
WebSocket HTTP rate limits honor Retry-After up to five minutes. The heartbeat
deadline defaults to 10 seconds and is independent of WebSocket ping/pong.
Message size is capped at 64 KiB and the library frame buffer at 1024, with
backpressure. Cancellation closes the connection and records disconnected status.

Run one Coinbase streaming worker per database. Conditional checkpoint updates
protect continuity against competing writers, but do not implement worker leases.

## Evidence and API

`GET /v1/sources` adds a `stream` object for live Coinbase: status (`connecting`,
`recovering`, `streaming`, `disconnected`), baseline/current trade IDs, update time,
and last heartbeat receipt. Replay datasets never expose or alter live checkpoints.
Status is last recorded process state, not a lease; after a crash, inspect its
timestamp and ordinary observation freshness. Heartbeats do not make old prices fresh.

WebSocket `received_at` is application consumption time, including buffering delay.
Its `latency_ms=0` means transport latency was not measured. REST recovery retains
page receipt times and request durations, so recovered observations cannot appear
in an analysis cutoff earlier than their actual receipt.

Raw provider objects remain in observations. Trade identity and default sequence
stay compatible with previous Coinbase REST evidence, keeping existing replay
checksums stable. This ingestion feature does not enable consensus or publication.
