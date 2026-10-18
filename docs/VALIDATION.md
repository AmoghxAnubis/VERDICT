# Batch 1 validation

Validated on Windows with a project-local Python 3.12 runtime.

## Passed

- Ruff lint and formatting checks.
- 46 automated tests: schema/precision, UTC, source isolation, versioned migrations,
  persistence across connections, duplicates/conflicts, atomic batch rollback,
  event-time ordering, replay hashes, API behavior, and mocked transport/retry cases.
- Actual SQLite migration and CLI imports of all three replay fixtures.
- Repeated baseline replay: first run inserts three unique events from four inputs;
  second run inserts none. Both return normalized SHA-256
  `77a5b28237b9d24a61cca41b9199f2118a613fb89b3ae5c421f8e5935d672cf6`.
- One public Coinbase ETH-USD ticker request returned HTTP 200 and stored a live
  observation through the ingestion command.
- Docker Compose configuration validation.

## Pending environment checks

The PostgreSQL integration test is skipped unless `VERDICT_TEST_DATABASE_URL` is
set. Docker's daemon was unavailable locally, so neither PostgreSQL runtime behavior
nor the container image build has been verified on this machine. CI is configured
to run the storage contract against PostgreSQL; CI has not been executed here.

These checks establish the observation foundation. Anomaly detection, confidence,
consensus, the dashboard, cryptographic proofs, and contracts are later milestones.

# Exchange and statistics slice

- Ruff lint and formatting checks plus 88 passing local tests; the optional
  PostgreSQL test remains skipped without its database URL.
- Populated SQLite database upgraded from 0001 to 0002, retaining existing evidence
  and backfilling receipt timestamps; fresh-schema/model agreement also checked.
- Public Coinbase, Kraken, and Binance.US requests each returned HTTP 200 and
  inserted live observations through independent workers.
- A fixed-cutoff live round correctly excluded Binance.US's roughly 230-second-old
  trade and reported insufficient data despite successful HTTP responses.
- Synthetic healthy, isolated-outlier, coordinated market-jump, and provider-outage
  fixtures are checked through real adapters, replay, snapshot storage, and analysis.
- Immutable round storage, dataset-scoped API retrieval, receipt-time boundaries,
  coverage-aware TWAP, zero MAD, retries, and failure/recovery counters are covered.

Docker/PostgreSQL runtime and CI execution remain unverified locally. The next
section records the subsequent oracle/DEX adapter integration.

# Six-provider oracle/DEX integration

Validated on 2026-10-18 using the project-local Python 3.12 runtime.

- Ruff lint and formatting checks; 147 tests pass, with the optional PostgreSQL
  integration test skipped without its database URL.
- Six-provider synthetic replay through the real normalizers, isolated SQLite
  storage, reopened connections, statistical analysis, immutable rounds, and API.
  All six observations remain inliers at `2026-10-16T00:00:10Z`; publication stays
  disabled. Repeated replay inserts zero additional observations.
- Mocked HTTP tests for Chainlink/Uniswap hash-pinned Ethereum reads, wrong-network
  rejection, all three requested block tags, RPC envelope identity/error handling,
  and reorg rejection. ABI selectors are checked against known interface selectors.
- Pyth exponent/integer/confidence handling, feed identity, retained hex evidence,
  configurable endpoint/Bearer authentication, and credential-free health/evidence.
- Uniswap pair/decimals/liquidity/lock checks, actual USDC/USD conversion (including
  a non-par rate), same-block conversion, and stale conversion rejection.
- Sequence tie handling in latest queries, snapshots, and round analysis. Migration
  0003 backfills existing sequence payloads and preserves evidence through upgrade,
  downgrade/re-upgrade, and model/schema checks. The 0001 upgrade path still passes.
- Independent six-source worker health, duplicate receipts, isolated failures,
  on-chain polling/retry intervals, and atomic rejection of malformed replay data.
- The dependency lock includes the Ethereum Keccak backend (`pycryptodome`),
  required for contract-call selectors in fresh installs.

The new live oracle/DEX endpoints have not been exercised in this validation.
Pyth requires an API key; RPC service support and availability must be checked in
the target environment. Docker/PostgreSQL runtime, CI execution, source-specific
freshness calibration, later reorg reconciliation, signature verification of Hermes
updates, and held-out historical validation remain unverified or unimplemented as
described in the roadmap and adapter documentation.
