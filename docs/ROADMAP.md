# VERDICT implementation roadmap

## Batch 1: observation foundation

- [x] MVP specification and development scope.
- [x] Python package, settings, logging, CLI, FastAPI service.
- [x] Decimal-safe ETH-USD observation model and UTC timestamps.
- [x] Raw/normalized SQLite and PostgreSQL storage with Alembic migration.
- [x] Public Coinbase REST connector and retrying polling worker.
- [x] Deterministic, isolated replay with duplicate/out-of-order scenarios.
- [x] Observation/history and freshness endpoints; unavailable feed response.
- [x] Automated checks and local/container startup documentation.

## Batch 2: ingestion coverage and statistical analysis

- [x] Add Coinbase streaming with reconnect and REST gap recovery.
- [x] Add Kraken and Binance.US direct ETH/USD adapters; verify provider availability.
- [x] Add Chainlink/Pyth reads and one well-defined Uniswap pool adapter.
- [x] Preserve chain, block/hash/finality, pair conversion, and lineage metadata.
- [x] Add persisted connector health, received/error counts, and consecutive failures.
- [x] Add Redis Streams with idempotent consumers and pending-message recovery.
- [x] Define synchronized round cutoffs and observation windows, including receipt-time filtering.
- [x] Implement median, MAD/zero-MAD handling, coverage-aware TWAP, RMS returns, and velocity.
- [x] Extend replay to multi-provider synthetic/adversarial cases and immutable diagnostic rounds.
- [ ] Validate against held-out historical data and market regimes.

Gate: comparable observations from six providers; explainable anomaly decisions.

Six adapters, block-pinned oracle/DEX reads, retained provenance, and six-provider
synthetic replay are implemented. Live six-provider availability and historical
calibration remain unverified. Optional Redis polling ingestion now includes SQL
receipt deduplication, commit-before-acknowledgement, and stale pending recovery;
real Redis/PostgreSQL runtime validation remains pending locally. Coinbase streaming now
has bounded REST recovery, durable checkpoints, and heartbeat/reconnect handling;
its public WebSocket/REST bootstrap was exercised live. Binance.US
is a distinct USD venue, not an implicit conversion of Binance ETH/USDT. A live
check found its latest trade stale despite HTTP 200; the engine correctly withheld
a three-source baseline instead of relaxing freshness requirements.
Pyth Hermes requires a configured API key. Chainlink's update cadence can exceed
the current 30-second freshness policy; a successful oracle read can remain stale.
The six-provider replay passes diagnostics but does not establish independent
upstream domains, calibrated confidence, or publication readiness.

Frozen exchange-only historical evaluation is now implemented: 21,000 retained
records, one calibration window, three held-out windows, and 1,440 diagnostic
rounds with paired interventions. March/April holdouts meet the three development
targets; the August stress holdout has no Binance.US ETHUSD records and stays
unavailable. Coordinated moves also produce transient exclusions. Historical
calibration remains open: no threshold was retuned, receipt times are assumed,
and historical oracle/DEX evidence and broader untouched regimes remain necessary.
See [methods and measured results](HISTORICAL_VALIDATION.md).

## Batch 3: dependencies, trust, consensus, confidence

- [x] Registry of known/unknown upstream dependencies with evidence and versions.
- [x] Historical consistency, uptime/freshness scoring, cold-start rules.
- [x] Quarantine/recovery hysteresis and dependency-aware weight limits.
- [ ] Weighted median with deterministic edge-case handling.
- [ ] Quality-score confidence breakdown and minimum availability gates.
- [ ] Immutable round records, explanation API, and history.
- [ ] Benchmarks covering isolated attacks, market moves, and correlated failures.

Gate: bad or insufficient evidence cannot produce a falsely healthy result.

The first dependency review retains the six adapters' implementation evidence,
explicit unknown upstreams, and typed transitive relationships. Immutable registry
versions, CLI registration/inspection, and read-only API access are implemented.
Shared Chainlink protocol, Ethereum/RPC, and collector dependencies are visible;
verified upstream independence and deployment matching remain pending.
Inspections always leave independent-domain count unknown and
publication disabled. See [registry scope and operation](DEPENDENCIES.md).

Fixed-grid diagnostic source scoring now records observed availability, freshness,
leave-one-source-out consistency, comparison coverage, and cold-start caps. Reports
pin the dependency registry, policy, and selected evidence, with separate immutable
storage and read-only history/detail endpoints. Three other fresh providers are
required for each comparison; provider independence is not inferred. This is an
uncalibrated quality indicator, not a voting weight or confidence probability.
See [scoring policy and evidence](SOURCE_SCORING.md).

Persistent quarantine/recovery and conservative dependency-aware weights now pin
each transition to retained scores, policies, registry evidence, and its previous
report. Immediate penalties, new-observation/time recovery gates, slow weight
increases, and final-share caps are implemented with serialized dataset histories.
All six bundled sources have unknown upstreams, so the shared unknown cap leaves
zero allocatable weight. Synthetic complete graphs exercise nonzero allocations
without asserting real independence. Weighted median and confidence/availability
gates remain next; publication stays disabled. See [weighting rules](WEIGHTING.md).

## Batch 4: dashboard and live updates

- [ ] Next.js dashboard, source detail, history, alerts, dependency graph.
- [ ] Round-linked WebSocket updates and reconnect/resume semantics.
- [ ] Clearly isolated simulation controls.
- [ ] Browser checks for healthy, degraded, unavailable, and recovery behavior.

Gate: the reason behind every accepted/rejected source is visible and reproducible.

## Batch 5: evidence and testnet integration

- [ ] Canonical evidence encoding, retention, Merkle trees, inclusion proofs.
- [ ] Domain-bound signed reports and key management/rotation policy.
- [ ] Oracle/consumer contracts, authorization, expiry, and replay protection tests.
- [ ] Select testnet, publication cadence, and deployment/runbook.
- [ ] Repeatable end-to-end manipulation/outage/recovery demonstration.

Gate: every testnet update traces back to retained evidence and a verified report.

## Later milestones

BTC/USD; operational hardening and observability; independently operated nodes;
validator agreement and governance; SDK/CLI expansion; additional data domains.
Staking/slashing, ZK, TEEs, and AVS require separate research and threat models.
