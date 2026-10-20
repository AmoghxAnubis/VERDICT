# VERDICT MVP specification

Status: initial implementation contract. This distinguishes requirements from
implemented behavior; the roadmap records implementation progress.

## Scope and milestones

1. Observation foundation: ETH/USD, Coinbase, raw/normalized persistence, replay, API.
2. Off-chain prototype: three exchanges, two oracle networks, one DEX, statistics,
   dependencies, trust, consensus, explanations, and dashboard.
3. Testnet oracle: retained evidence, Merkle commitments, signed reports, consumer.
4. Expansion: BTC/USD and operational hardening, followed by independent validators.

The initial node is centrally operated. Distributed operation is a later milestone.
ML, staking/slashing, ZK proofs, TEEs, AVS, and non-price resolution are outside MVP.

## Observation contract (implemented)

- Instrument: `ETH-USD`, ETH base and actual USD quote. USDT, USDC, and wrapped
  assets require explicit conversion observations and provenance before inclusion.
- Dataset: `live` or `replay:<scenario>`. Live is the API default; replay cannot
  write into live. Scenario names are lowercase letters/numbers, `_`, or `-`.
- Providers: Coinbase Exchange public ETH-USD ticker, Kraken XETHZUSD recent trades,
  Binance.US ETHUSD recent trades, Chainlink ETH/USD, Pyth Hermes ETH/USD, and
  Uniswap v3 USDC/WETH 0.05% with same-block Chainlink USDC/USD conversion.
  Exchanges report last trades; Uniswap reports pool spot; oracle update cadence
  differs. See [adapter evidence](ORACLE_ADAPTERS.md). Provider count does not
  establish independent upstream domains.
- Value: positive finite decimal, up to 38 digits and 18 fractional places.
  Binary floating-point prices are rejected; JSON output uses decimal strings.
- Time: provider event time and receipt time are both timezone-aware, normalized
  to UTC. Request latency is measured separately with a monotonic clock.
- Identity: SHA-256 of schema version, dataset, provider, instrument, and provider
  event ID. This identity is not an authenticity guarantee.
- Storage: first receipt and raw JSON object retained per unique trade event.
  A duplicate does not overwrite evidence. Reusing an identity with a changed
  price or event timestamp is rejected and surfaced as a conflict.
- Ordering: provider event timestamp, then provider sequence, then observation ID
  as a deterministic tie breaker. Sequence is the Pyth slot or Uniswap block
  number and defaults to zero for providers without a sequence. Out-of-order
  arrivals never replace a newer event as the latest.
- Clock policy: the live connector rejects timestamps more than five seconds in
  the future by default. Old data is retained as evidence and marked stale.
- Freshness: source age is computed from event time. The initial 30-second limit
  is a configurable development policy, not a calibrated market safety threshold.
- Replay: fixed fixture timestamps and request latencies; no wall-clock values or
  network calls enter normalized output. Query-time age still uses current time.
  Each fixture is validated first, then committed atomically. A conflicting event
  rolls back the entire replay import.

Schema migrations precede API/worker startup. SQLite is the local fallback;
PostgreSQL is the shared deployment store. No network ingestion runs on import.

## Aggregation contract (partially implemented)

The statistical subset is implemented separately as described in
[the statistical policy](STATISTICAL_ENGINE.md). It does not enable consensus,
confidence, or on-chain publication. Diagnostic rounds are retained with fixed
cutoffs, configuration hashes, input IDs, and per-source explanations.

An aggregation round must fix its cutoff time, eligible observation window,
source configuration, dependency graph version, and algorithm version. One eligible
observation per provider/instrument is selected at the cutoff, without looking ahead.

Before enabling output, implement and validate:

1. Source-specific freshness and future-clock rules; missing and invalid fields.
2. Median/MAD baseline, a defined zero-MAD fallback, volatility/velocity/TWAP windows.
3. Cold-start reputation, fast penalties, slow recovery, and quarantine hysteresis.
4. Documented upstream domains, unknown-dependency penalties, and weight caps.
5. Weighted median with deterministic tie behavior and explicit zero-total-weight
   handling. Record both preliminary and final weights and all exclusions.
6. A confidence quality score with a published breakdown. No probability-of-truth,
   manipulation probability, or statistical confidence interval without validation.
7. Availability policy: proposed starting gate is at least three eligible upstream
   domains. Validate this and concentration limits in replay before enabling feeds.

Agreement with consensus is historical consistency, not independently known truth.
An outlier detector cannot guarantee resistance to a coordinated majority. Shared
upstream domains must not gain voting weight from multiple downstream wrappers.
Unknown relationships remain unknown; price correlation alone does not prove lineage.

The [versioned dependency registry](DEPENDENCIES.md) now retains implementation
evidence and explicit unknowns, with deterministic transitive inspection and
immutable version storage. It does not yet establish independent domains or bind
the legacy statistical round schema to a registry. Separate scoring, weighting,
and aggregation reports pin their selected registry.

[Diagnostic source scoring](SOURCE_SCORING.md) now implements fixed-grid observed
availability, freshness, peer consistency, and cold-start caps. Its separate saved
reports pin the dependency registry and source evidence. These uncalibrated scores
feed [persistent quarantine/recovery and dependency caps](WEIGHTING.md).
[Weighted-median candidates](AGGREGATION.md) now retain deterministic half-weight
ties, zero-weight handling, and complete input explanations. Confidence and domain
availability gates remain pending. Existing statistical rounds and the frozen
historical benchmark remain unchanged.

## Result and failure contract

Until consensus output gates are implemented, `/v1/feed/ETH-USD` returns HTTP 503 with
`value=null`, `confidence=null`, and `reason=consensus_engine_not_implemented`.
Observations remain available separately.

Future rounds return a round ID, asset, decimal value, basis-point confidence
(0..10000), event/cutoff timestamps, operational status, evidence reference, and
configuration version. API and contract confidence use the same integer unit.
The document's display bands become non-overlapping: [0,5000), [5000,7500),
[7500,9000), [9000,9800), and [9800,10000]. These bands do not establish permission
to publish or authorize a consumer action by themselves.

Below availability requirements, return `unavailable` with no usable current value.
Historical last-good results keep their original timestamp and explicit expiry.
Consumers independently enforce status, confidence, and maximum age. Consumer
actions such as pausing new borrowing belong in application policy, not a universal
VERDICT rule. Recovery needs hysteresis to avoid repeated mode switching.

## Evidence and publication contract (testnet milestone)

Retain selected/rejected observations, scores, weights, upstream evidence,
algorithm/configuration versions, and explanations for every round. Specify
canonical encoding, leaf ordering, and Merkle inclusion proofs. A commitment
proves inclusion of committed evidence; it does not prove truth or correct execution.

Signed reports bind feed, value, confidence, status, timestamp, expiry, round,
evidence root, chain ID, contract address, and configuration version. Contracts
verify authorization, freshness, ordering, and replay protection. Key rotation,
retention, and publication cadence must be specified before testnet deployment.

## Validation gates

The first slice must pass precision/schema validation, persistence across reopened
connections, duplicate/conflict handling, out-of-order ordering, isolated replay,
deterministic replay hashes, API validation/readiness, and mocked transport failures.

Before enabling consensus, set measured targets for error under defined attacks,
false exclusions during market-wide moves, detection delay, availability, and
recovery. Use both controlled fixtures and held-out historical periods. Record
the policy and benchmark version before evaluating results; do not tune to demo
numbers from the vision document.
