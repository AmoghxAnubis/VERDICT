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

- [ ] Add Coinbase streaming with reconnect and REST gap recovery.
- [x] Add Kraken and Binance.US direct ETH/USD adapters; verify provider availability.
- [x] Add Chainlink/Pyth reads and one well-defined Uniswap pool adapter.
- [x] Preserve chain, block/hash/finality, pair conversion, and lineage metadata.
- [x] Add persisted connector health, received/error counts, and consecutive failures.
- [ ] Add Redis Streams with idempotent consumers and pending-message recovery.
- [x] Define synchronized round cutoffs and observation windows, including receipt-time filtering.
- [x] Implement median, MAD/zero-MAD handling, coverage-aware TWAP, RMS returns, and velocity.
- [x] Extend replay to multi-provider synthetic/adversarial cases and immutable diagnostic rounds.
- [ ] Validate against held-out historical data and market regimes.

Gate: comparable observations from six providers; explainable anomaly decisions.

Six adapters, block-pinned oracle/DEX reads, retained provenance, and six-provider
synthetic replay are implemented. Live six-provider availability and historical
calibration remain unverified; streaming and Redis recovery remain open. Binance.US
is a distinct USD venue, not an implicit conversion of Binance ETH/USDT. A live
check found its latest trade stale despite HTTP 200; the engine correctly withheld
a three-source baseline instead of relaxing freshness requirements.
Pyth Hermes requires a configured API key. Chainlink's update cadence can exceed
the current 30-second freshness policy; a successful oracle read can remain stale.
The six-provider replay passes diagnostics but does not establish independent
upstream domains, calibrated confidence, or publication readiness.

## Batch 3: dependencies, trust, consensus, confidence

- [ ] Registry of known/unknown upstream dependencies with evidence and versions.
- [ ] Historical consistency, uptime/freshness scoring, cold-start rules.
- [ ] Quarantine/recovery hysteresis and dependency-aware weight limits.
- [ ] Weighted median with deterministic edge-case handling.
- [ ] Quality-score confidence breakdown and minimum availability gates.
- [ ] Immutable round records, explanation API, and history.
- [ ] Benchmarks covering isolated attacks, market moves, and correlated failures.

Gate: bad or insufficient evidence cannot produce a falsely healthy result.

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
