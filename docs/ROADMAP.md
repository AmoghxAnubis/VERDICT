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
Real PostgreSQL 18.1 storage/runtime validation now passes locally, including a
receipt-deduplication fix; Redis and the Compose PostgreSQL 17 runtime remain
unverified. See [runtime checks](RUNTIME_VALIDATION.md). Coinbase streaming now
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
- [x] Weighted median with deterministic edge-case handling.
- [x] Quality-score confidence breakdown and minimum availability gates.
- [x] Immutable round records, explanation API, and history.
- [x] Benchmarks covering isolated attacks, market moves, and correlated failures.

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
without asserting real independence. See [weighting rules](WEIGHTING.md).

Diagnostic weighted-median aggregation now uses pinned scoring observations and
final weights, with lower-price half-weight ties and explicit zero-total handling.
Separate immutable reports, CLI persistence, and dataset-scoped explanation/history
endpoints retain the complete samples, weights, and exclusions. Saving reproduces
the candidate and validates retained payloads and selection boundaries.
See [aggregation rules](AGGREGATION.md).

Quality assessments now retain seven component scores and ten minimum-availability
gate decisions, with exact integer/rational arithmetic, registry age checks, and
source/economic/unknown concentration limits. Separate immutable assessment records,
CLI persistence, and dataset-scoped explanation/history endpoints are implemented.
Independent-domain evidence is unsupported by registry v1 and fails explicitly as
unknown, even for synthetic complete graphs. Thus a high diagnostic quality score
cannot produce a usable value or feed confidence. Versioned independence/deployment
evidence remains necessary; the full-pipeline benchmarks are described below.
The live feed and publication stay disabled. See [quality and gates](CONFIDENCE.md).

Full-pipeline synthetic benchmarks now retain 400 rounds across eight scenarios and
two explicitly distinguished dependency profiles. Isolated spikes and a correlated
pair are quarantined, outages/delays expire under freshness policy, and recovery is
measured. A correlated four-provider majority shifts the candidate by 35% and fails
the frozen attack target; every availability assessment still blocks usable output.
These shortened test policies do not establish default-policy or historical
calibration, verified independence, or coordinated-majority resistance. The Batch 3
implementation checklist is complete; its real-evidence release gate remains open.
See [benchmark methods and results](ADVERSARIAL_BENCHMARKS.md) and the
[remaining delivery estimate](DELIVERY_ESTIMATE.md).

## Batch 4: dashboard and live updates

- [x] Next.js dashboard, source detail, history, alerts, dependency graph.
- [x] Round-linked WebSocket updates and reconnect/resume semantics.
- [x] Clearly isolated simulation controls.
- [x] Browser checks for healthy, degraded, unavailable, and recovery behavior.

Gate: the reason behind every accepted/rejected source is visible and reproducible.

Ten production-build browser checks pass on 25 October 2026 using installed Chrome
on Windows, at desktop and emulated mobile sizes. Real retained replay evidence
exercises source details, history, dependencies, exports, attack/outage recovery,
and cursor-based WebSocket reconnects after a separate process commits a round.
HTTP failure/retry and mobile overflow are covered. The checks found and fixed a
hidden table label escaping its scroll container. CI now has a separate browser
workflow; its hosted execution remains unverified locally. This completes the
diagnostic dashboard gate; publication and real-evidence gates remain open.
See [dashboard operation and checks](DASHBOARD.md).

## Batch 5: evidence and testnet integration

- [x] Canonical evidence encoding, retention, Merkle trees, inclusion proofs.
- [x] Domain-bound signed reports and key management/rotation policy.
- [x] Oracle/consumer contracts, authorization, expiry, and replay protection tests.
- [x] Select testnet, publication cadence, and deployment/runbook preparation.
- [x] Repeatable local end-to-end manipulation/outage/recovery demonstration.
- [ ] Execute and independently verify the public Sepolia deployment/demonstration.

Gate: every testnet update traces back to retained evidence and a verified report.

The first Batch 5 slice exports a self-contained round evidence bundle, including
the complete weighting predecessor chain, scoring inputs, and full retained
dependency evidence. A versioned typed byte codec, domain-separated SHA-256 tree,
count-bound roots, and inclusion proofs are implemented. Offline verification
reproduces the pipeline; exports and proof files refuse overwrites. Existing SQL
evidence is unchanged, and no retention expiry or publication path is added.
Public testnet deployment remains pending.
See [encoding, proof rules, and retention limits](EVIDENCE.md).

Offline EIP-712 diagnostic reports now bind the domain, dataset, evidence root,
retained configuration, observation/issuance/expiry times, round, and signer ID.
V1 preserves unavailable/null output. Public signer registry successors support
bounded rotation overlap and verification-time revocation, with independently
pinned registry hashes and no resurrection of retired identities. Encrypted local
development keystores and an offline CLI are implemented. Production key custody,
administrative registry distribution, and public testnet operations remain separate
gates. See [signing and rotation](SIGNING.md).

On 26 October 2026, 33 focused signing checks and the full 528-test backend
regression suite pass locally; six optional PostgreSQL/Redis checks are skipped.
An independent Solidity ABI encoding agrees with the signed EIP-712 digest.

Local Solidity oracle/consumer contracts now execute the same EIP-712 reports on
Py-EVM, enforce current signer windows/revocations, expiry, strictly increasing
rounds, assessment/digest replay protection, immutable policy context, and two-step
administration. Full registry snapshots are monotonic and emergency stops cannot
be undone by later snapshots. V1 accepts unavailable diagnostics only; the consumer
always withholds usable price reads. On 26 October 2026, 59 contract checks and
the full 587-test suite pass, with six optional PostgreSQL/Redis checks skipped.
Compiler artifacts reproduce from pinned sources. Public deployment and the
testnet manipulation/outage/recovery demonstration remain pending. See
[contracts and verified ABI exports](CONTRACTS.md).

Sepolia is selected with a separate replay dataset, a 30-second publication
cadence, 120-second diagnostic transport bounds, and a 45-second submission
margin. A pinned profile, checksum-bound unsigned type-2 transaction plans,
read-only network/state/simulation preflight, and exact transaction/receipt/event
audits are implemented. Gas/fee budgets, an eight-entry initial registry limit,
wallet handoff, confirmations/finality, nonce/reorganization recovery, and retained
artifacts are documented in the [testnet runbook](TESTNET_RUNBOOK.md). No automatic
publisher or broadcaster is added. Public RPC compatibility, wallet execution,
deployment hashes, and the public timed manipulation/outage/recovery demonstration remain
unverified and belong to the next gate. Local preparation does not enable usable
feed publication.

On 26 October 2026, 64 preparation/RPC checks and the full 651-test backend suite
pass locally, with six optional PostgreSQL/Redis checks skipped. Compiler
reproducibility, lint, formatting, and frozen dependency checks pass. The local
eight-key initialization probe fits the two-million-gas default plan; actual
Sepolia gas and wallet execution still require public verification.

A continuous replay now retains five checkpoints through the complete diagnostic
pipeline, signed reports, preflight, local contract execution, and exact receipt
audits. The isolated EVM run executes eight transactions and checks duplicate,
changed-root, and expired-report rejection plus unavailable consumer behavior.
An independently pinned offline archive verifier reproduces evidence/signatures
and transaction/event linkage; a current-clock evidence command prepares the same
session for later public execution. Replay provenance is explicitly synthetic, and
the demo has its own migrated database. See [demonstration commands and scope](TESTNET_DEMO.md).
Local execution is complete; the Batch 5 public testnet gate remains open pending
wallet/RPC setup, real transaction hashes, and independent chain verification.

## Later milestones

BTC/USD; operational hardening and observability; independently operated nodes;
validator agreement and governance; SDK/CLI expansion; additional data domains.
Staking/slashing, ZK, TEEs, and AVS require separate research and threat models.
