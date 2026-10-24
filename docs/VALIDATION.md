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
- Actual CLI migration, replay, repeat import, fixed-cutoff analysis, and ingestion
  help checks pass against an isolated SQLite database. The original baseline
  checksum remains `77a5b28237b9d24a61cca41b9199f2118a613fb89b3ae5c421f8e5935d672cf6`;
  six-provider replay yields
  `485eede9dad563aa1db6321364c3be1b01f556a636887abdce64ab3bd57c82e1`.

The new live oracle/DEX endpoints have not been exercised in this validation.
Pyth requires an API key; RPC service support and availability must be checked in
the target environment. Docker/PostgreSQL runtime, CI execution, source-specific
freshness calibration, later reorg reconciliation, signature verification of Hermes
updates, and held-out historical validation remain unverified or unimplemented as
described in the roadmap and adapter documentation.

# Coinbase streaming integration

Validated on 2026-10-19 with the project-local Python 3.12 runtime.

- 193 automated tests pass; the optional PostgreSQL integration test remains
  skipped without its database URL. Ruff lint and formatting checks pass.
- Mocked sessions exercise initial subscription, last-match handling, duplicate and
  out-of-order overlap, match/heartbeat gaps, reconnect/resubscribe, cancellation,
  and a heartbeat deadline independent of unrelated messages.
- REST recovery tests exercise opaque cursor pagination, overlapping pages, missing
  trades, response ordering, lagging REST, page bounds, and conflicting triggering
  matches. Failed recovery leaves retained evidence and checkpoints unchanged.
- Checkpoint/evidence/health writes commit atomically, conflicting batches roll back,
  and stale workers cannot overwrite a newer checkpoint. Reopening the database
  retains the baseline and current trade IDs.
- Migration 0003 to 0004 preserves existing observations, health, and immutable
  rounds; schema/model agreement passes. Existing earlier upgrade paths also pass.
- CLI argument validation, continuous/one-shot routing, provider independence,
  and live/replay API isolation are covered. The optional PostgreSQL contract now
  includes checkpoint writes, but has not been run locally.
- A bounded live public Coinbase check subscribed successfully, bootstrapped from
  REST trade 849170134, retained three subsequent stream trades through 849170137,
  and received a heartbeat with zero connector errors. It used an isolated SQLite
  database and cancelled the worker afterward.

Live long-outage recovery, sustained-load behavior, and PostgreSQL runtime remain
unverified. Reconnect and gap failure cases were tested with controlled transports;
the live check establishes connectivity and basic stream/REST interoperability.
Redis Streams and held-out historical calibration remain later Batch 2 work.

# Redis polling ingestion

Validated on 2026-10-19 with the project-local Python 3.12 runtime.

- 249 automated tests pass; real Redis and PostgreSQL integration tests are
  skipped without their service URLs. Ruff lint and formatting checks pass.
- All six polling adapters produce versioned envelopes; an end-to-end CLI test
  queues and consumes six observations through the real SQLite store and API,
  preserving decimal precision, receipt timestamps, provenance, and replay isolation.
- Actual migrations from 0004 to 0005 preserve observations, connector health,
  and Coinbase checkpoints. Fresh-schema/model agreement and earlier upgrade paths
  pass. The SQL inbox survives reopened connections and concurrent receipt claims.
- Redis emulation exercises pre-consumer backlog, idle claims and advancing scan
  cursors, active-consumer protection, ambiguous publish replies, and crashes before
  commit or after commit but before acknowledgement. Redelivery changes neither
  evidence nor health counters; distinct repeated polls still count separately.
- Database failure rolls back receipts/evidence/health and leaves messages pending.
  Conflicts, malformed payloads, missing attempt identities/times, unknown versions,
  and replay injection are rejected without acknowledgement. Valid entries still
  progress; removed pending payloads cause a visible failure.
- Queue producer CLI startup requires no database. Redis/direct selection,
  incompatible streaming arguments, one-shot rejection exit codes, cancellation,
  sanitized errors, and capped queue retries are covered. Compose configuration
  validation passes for the optional queue profile.
- CI now provisions Redis alongside PostgreSQL and includes an actual Redis
  delivery/reclaim contract and the PostgreSQL SQL-inbox contract.

The Docker daemon is unavailable locally. Real Redis/PostgreSQL service behavior,
container builds, CI execution, persistence under host failure, sustained load,
and failover have not been verified here. Redis tests use an emulator locally.
Coinbase WebSocket ingestion retains direct checkpoint writes; only polling uses
the optional queue. See [queue operation and limits](REDIS_INGESTION.md).
Held-out historical calibration remains the next open Batch 2 implementation task.

# Historical exchange evaluation

Validated on 2026-10-19 with the project-local Python 3.12 runtime.

- 270 automated tests pass; the optional real Redis/PostgreSQL service tests remain
  skipped without their URLs. Ruff lint and formatting checks pass.
- Actual public Coinbase, Kraken, and Binance.US requests retained twelve
  compressed captures, containing 21,000 normalized records across four disjoint
  historical windows and their warmup histories. The suite and policy were frozen
  before full collection and evaluation; no detection/freshness threshold was tuned.
- The offline CLI produced 1,440 diagnostic rounds with baseline and five paired
  intervention runs. It opens neither a database nor a network connection.
  Report, policy, suite, source-code, and capture checksums are retained. Eight real
  first/last baseline rounds reproduce from the archives in automated checks;
  synthetic suite evaluations repeat identically under a different Decimal context.
- Tests cover literal decimal/epoch retention, original provider payloads, assumed
  receipt times, temporal split/warmup separation, cutoff filtering of late inputs,
  range completion, corrupt/truncated archives, cursor stalls, same-millisecond
  aggregate pagination, retries/request budgets, and resume without overwriting.
- March/April held-out baseline availability is 49/60 and 52/60 rounds (81.7% and
  86.7%). All 14 eligible +35% Coinbase injection opportunities are excluded; zero
  natural extreme-deviation exclusions are observed among eligible comparisons.
  These are small-sample conditional measurements, not labelled accuracy estimates.
- January and August return no Binance.US ETHUSD records, produce no three-source
  baseline, and leave attack metrics unscored. August's roughly 3.3% decline and
  5% intrawindow range therefore do not establish stress-period detection quality.
- The coordinated +10% step causes five additional source exclusions across the
  March/April samples. Correlated +35% shifts move the diagnostic median by up to
  3,500 basis points. These results record current agreement/availability limits.
- Strict CLI mode writes the reviewable report and exits unsuccessfully when any
  held-out target is unmet or unscored. The retained run reports
  `heldout_targets_met=false`, `six_provider_validation_complete=false`, and
  `publication_ready=false`.

The January window cannot calibrate a three-source policy. Historical first-receipt
times are unavailable and explicitly assumed at event time plus 250 milliseconds;
oracle/DEX history and longer independently labelled regimes remain missing.
Historical validation remains open, and the live feed stays unavailable. See
[methods, detailed results, and reproduction commands](HISTORICAL_VALIDATION.md).

# Dependency registry foundation

Validated on 2026-10-19 with the project-local Python 3.12 runtime.

- 301 automated tests pass; the optional real Redis/PostgreSQL service tests remain
  skipped without their URLs. Ruff lint and formatting checks pass.
- The bundled six-provider review retains eight connector implementation snapshots
  and a limitations note. Tests compare the retained code to the reviewed files and
  check every oracle/DEX provenance dependency against the graph's transitive closure.
- Transitive inspection exposes shared Chainlink protocol, Ethereum, RPC configuration,
  and collector dependencies, while preserving unknown memberships and operator details.
  Selected-source ordering and UTC-equivalent timestamps produce identical hashes.
- Invalid/cyclic graphs, broken references, duplicate identities, changed evidence,
  future evidence timestamps, and unvalidated model copies are rejected. Immutable
  version conflicts, concurrent idempotent registration, reopened storage, old-version
  retrieval after a new review, and corruption detection are exercised.
- Migration 0005 to 0006 preserves observations, diagnostic rounds, and queue receipts.
  Fresh-schema/model agreement and prior migration paths pass. CLI registration and
  inspection match read-only API results; missing/invalid IDs and source sets fail
  explicitly. The optional PostgreSQL contract includes registry storage and conflicts.
- The package build includes the retained registry resource. Its packaged document
  validates to the same hash as the source checkout; the build also produces an sdist.

This review establishes implementation dependencies, not upstream independence or
deployment authenticity. It does not modify the frozen historical evaluation or
statistical policy. Historical calibration, real Redis/PostgreSQL runtime checks,
evidence refresh/deployment matching, trust/weight policy, and consensus remain open.
Every dependency inspection keeps independent-domain count null and publication
disabled. See [scope, evidence, and version selection](DEPENDENCIES.md).

# Diagnostic source scoring

Validated with the project-local Python 3.12 runtime.

- 332 automated tests pass; the optional real Redis/PostgreSQL tests remain skipped
  without their service URLs. Ruff lint and formatting checks pass.
- Fixed-grid source scoring is exercised against synthetic established history,
  cold start, repeated trades, isolated outliers, coordinated moves, correlated
  majorities, outages, recovery, insufficient peers, and missing comparison history.
  Tests explicitly demonstrate majority-agreement limits without enabling publication.
- Receipt/event boundaries, fractional freshness/skew limits, old late arrivals,
  pre-window anchors, ordering ties, per-source timing, and deterministic hashes
  under reordered input and changed Decimal context are covered. Pure evaluation
  and SQL-backed historical selection produce identical reports.
- Comparison excludes the target, requires three other fresh providers by default,
  and distinguishes unavailable data from unscored consistency. Missing comparison
  history limits the composite score; repeated observations do not mature cold start.
- Report/policy hashes, pinned registry versions, exact retained input evidence,
  atomic rejection of missing evidence, concurrent idempotent saves, reopened
  connections, storage corruption detection, CLI policy validation, and read-only
  dataset-scoped API retrieval are covered.
- Migration 0006 to 0007 preserves observations and dependency registries; earlier
  upgrade paths and schema/model agreement still pass. The PostgreSQL contract now
  includes scoring selection and persistence but has not been run locally.
- The frozen historical report's existing source/capture checksums and reproduced
  rounds continue to pass. No statistical policy or historical result was retuned.
- An actual CLI smoke run migrated an isolated SQLite database, registered the
  dependency review, replayed the six-provider fixture, and saved/repeated scoring.
  Both executions returned the same report ID, all six sources remained in cold
  start, and publication stayed disabled. The built wheel loads its scoring module
  and bundled registry independently of the source checkout.

These are uncalibrated diagnostic quality scores. Real service integration,
held-out six-provider calibration, independent upstream evidence, deployment
matching, quarantine/recovery, and dependency-aware weighting remain open. The
scoring API does not supply consensus confidence, and the feed remains unavailable.
See [scoring formulas, cold-start rules, and evidence](SOURCE_SCORING.md).

# Quarantine and dependency-constrained weights

Validated on 2026-10-20 with the project-local Python 3.12 runtime.

- 364 automated tests pass; real Redis and PostgreSQL tests remain skipped without
  their service URLs. Ruff lint and formatting checks pass.
- Source states cover cold-start probation, immediate quarantine on unavailable,
  outlier, or unscored evidence, hysteresis, recovery resets, distinct-observation
  and elapsed-time gates, gradual increases, and immediate quality decreases.
  Missing weighting rounds quarantine all sources instead of skipping recovery.
- Deterministic integer allocation checks cover overlapping economic/source/unknown
  caps, final normalized shares, floor rounding, no redistribution of suppressed
  mass, and infeasible allocations. All six bundled sources have unresolved
  upstreams, so their shared unknown cap correctly leaves zero allocatable weight.
- SQLite persistence covers reopened reads, retained policy/registry/score hashes,
  concurrent duplicate and competing successor applications, transactional rollback,
  stale cutoffs, and immutable dataset/policy selection. Missing, reset, older, or
  dangling dataset heads are rejected before they can fork history. Dataset-scoped
  API reads and invalid requests are exercised; publication remains disabled.
- Migration 0007 to 0008 preserves scoring evidence and matches the SQLAlchemy
  schema. Earlier upgrade paths pass. The optional PostgreSQL contract now includes
  concurrent weighting duplicates and successor persistence; it was not run locally.
- An actual subprocess CLI smoke run migrated an isolated SQLite database,
  registered the dependency review, replayed the six-provider fixture, scored it,
  and applied weighting twice with an identical report ID. All sources stayed in
  probation with zero weight. A reopened API returned that report only in its
  replay dataset, reported readiness, and kept the live feed at HTTP 503.

These checks validate development state/weight rules, not calibrated consensus.
Real Redis/PostgreSQL runtime, six-provider historical calibration, upstream
independence, and deployment matching remain unverified. Weighted median,
quality/confidence and availability gates, and publication remain subsequent work.
See [weighting policy, recovery rules, and limits](WEIGHTING.md).

# Diagnostic weighted-median aggregation

Validated on 2026-10-20 with the project-local Python 3.12 runtime.

- 408 automated tests pass, including 44 aggregation checks. The real PostgreSQL
  and Redis tests remain skipped without service URLs. Ruff lint and formatting
  checks pass.
- Median tests cover exact half-weight lower-price selection, unequal/odd weights,
  repeated prices, zero weights, empty inputs, small and large decimal values,
  input permutations, and changed Decimal precision/rounding. Invalid prices and
  noninteger/negative/oversized weights are rejected.
- Synthetic complete-dependency history exercises nonzero candidates through real
  source scoring, recovery, weighting, SQLite persistence, CLI, and API. Current
  outliers and stale sources retain their evidence and exclusions with zero weight.
  Cold start, probation, missing history, and the bundled unknown pool produce no
  candidate. No synthetic graph is treated as real independence evidence.
- Inconsistent positive-weight inputs fail instead of being removed and rescaled.
  Score/weight identity mismatches, invalid model copies, forged candidate values,
  changed input checksums, and missing or changed retained evidence are rejected.
  Even a self-consistent score/weight pair cannot substitute a different value for
  the original observation payload during aggregation persistence.
- Concurrent saves are idempotent; failed evidence validation rolls back inserts.
  Reopened reads, immutable metadata/hash checks, dataset isolation, newest-first
  history, aggregation of older rounds, and stability after later arrivals pass.
- Migration 0008 to 0009 preserves weighting reports and the dataset head, with
  schema/model agreement and readiness before/after migration. Earlier upgrade
  paths also pass. The optional PostgreSQL contract includes concurrent aggregation
  saves and scoped retrieval, but was not executed locally.
- An actual subprocess CLI smoke run migrated an isolated SQLite database,
  registered dependency evidence, replayed six providers, scored, weighted, and
  aggregated twice. Both aggregation runs returned the same report ID, with null
  candidate and unavailable status. A reopened API returned the retained report
  only in its replay dataset and kept the live feed at HTTP 503.

Confidence and independent-domain availability gates remain pending. All candidate
reports retain null confidence/domain count and `publication_ready=false`.
These checks do not establish calibrated accuracy or coordinated-attack resistance.
See [aggregation algorithm, evidence, and limits](AGGREGATION.md).

# Quality breakdown and availability gates

Validated on 2026-10-20 with the project-local Python 3.12 runtime.

- 445 automated tests pass, including 37 confidence checks. Real PostgreSQL and
  Redis service tests remain skipped without their URLs. Ruff lint and formatting
  checks pass.
- Synthetic complete-graph history produces quality 9,700/10,000 with six weighted
  providers and all measured gates passing except independent domains. The domain
  observation stays unknown, and usable value/confidence remain null. A coordinated
  price move can also score highly without bypassing this gate.
- Exact weighted components, allocated-budget penalties, maximum-deviation
  agreement, pooled unknown shares, economic/source concentration, and inclusive
  integer gate thresholds are checked. Registry time boundaries retain microsecond
  precision. Caller Decimal precision and rounding do not change report JSON.
- Outliers remain visible with zero contributing weight. Outages return zero
  quality and unknown concentration rather than apparently healthy zero exposure.
  Policy validation rejects disabled domain/source minimums and override fields.
  Forged quality, gate decisions, domain counts, and usable output are rejected.
- SQLite tests cover concurrent idempotent saves, policy variants, reopened reads,
  scoped history/detail APIs, corruption detection, and CLI policy loading. New
  assessments require the retained aggregation, score, weights, registry, and exact
  selected observations. Missing or changed evidence rolls back the insertion,
  including original payloads changed after aggregation was saved.
- Migration 0009 to 0010 preserves saved aggregation and matches the ORM schema.
  Earlier migrations and all 44 aggregation checks continue to pass after sharing
  evidence validation. The optional PostgreSQL contract now includes concurrent
  confidence saves and dataset-scoped retrieval; it was not executed locally.
- A subprocess CLI smoke run migrated isolated SQLite storage, retained six-provider
  replay/scoring/weighting/aggregation, and applied confidence twice. Both runs
  produced the same assessment ID, ten gates, zero quality, and null usable output.
  Reopened API reads retained dataset isolation and kept the feed at HTTP 503.

These are uncalibrated development assessments. Registry v1 has no independent-domain
attestation contract, so that gate always fails as unknown, including when unknown
nodes are removed from a synthetic graph. Independent-domain and deployment
evidence, held-out six-provider calibration, full-pipeline adversarial benchmarks,
and usable feed output remain open. Publication stays disabled.
See [formulas, policy, and evidence limits](CONFIDENCE.md).

# Full-pipeline adversarial benchmarks

Validated on 2026-10-24 with the project-local Python 3.12 runtime.

- 461 automated tests pass, including 16 benchmark checks. The optional real Redis
  and PostgreSQL tests remain skipped without service URLs. Ruff lint and
  formatting checks pass. Tests ran with Windows temporary-directory access;
  the existing sandbox temp-directory permissions prevented the initial run.
- The offline CLI retained 400 rounds across eight six-provider scenarios and two
  explicit registry profiles. Every round executes scoring, stateful weighting,
  aggregation and quality/availability gates. Bundled unknowns produce zero weight;
  the synthetic profile exercises positive candidates without independence claims.
- Retained results reproduce exactly, including under reversed fixture input order
  and a changed caller Decimal precision/rounding context. Fixture/temporal
  validation, event/receipt cutoff selection, delayed-data visibility, quarantine,
  recovery, null usable output and all independent-domain failures are checked.
- Single-spike candidate shift is at most one basis point, with immediate
  quarantine and first positive recovery weight 50 seconds after intervention end.
  Shared outages and delayed receipts trigger quarantine 20 seconds after onset.
  A coordinated market move produces no extra quarantine decisions in this grid.
- The correlated-majority intervention shifts the candidate by 3,500 basis points,
  exceeding the frozen five-basis-point target. The actual strict CLI run writes
  the retained report and exits with code 1. All 400 usable outputs still remain
  null and publication stays disabled; the test suite asserts this measured failure.

These are synthetic measurements under explicitly shortened test policies.
Production-default/historical calibration, independent upstream/deployment evidence,
real service runtime, load and live outage behavior remain unverified. The backend
benchmark implementation milestone is complete; release evidence remains open.
See [benchmark design and measurements](ADVERSARIAL_BENCHMARKS.md) and the
[remaining delivery estimate](DELIVERY_ESTIMATE.md).

# Actual PostgreSQL runtime and queue receipt fix

Validated on 2026-10-24 using installed PostgreSQL 18.1 on Windows, with a new
localhost-only cluster and database for every run.

- The full suite passes **466 tests**, including all five actual PostgreSQL checks;
  only the real Redis contract is skipped. Ruff lint and formatting checks pass.
- The first actual storage run exposed receipt redelivery recounting health.
  A rolled-back diagnostic probe confirmed psycopg reports `rowcount=-1` for both
  inserted and conflicting receipt inserts. Claims now use `RETURNING id`, with
  no returned row treated as a duplicate. SQLite and PostgreSQL regressions pass.
- Actual concurrent success/error claims apply one effect across four consumers.
  Reopened connections preserve deduplication, changed attempt contents fail, and
  new attempts remain distinct. Observation conflicts roll back receipts and health;
  a corrected redelivery can claim that attempt afterward.
- PostgreSQL runs every migration through `0010` and validates ORM/schema agreement,
  retained evidence, concurrent report persistence, weighting successors, health,
  SQL inbox and Coinbase checkpoints. A separate 25-round synthetic pipeline with
  positive weights reproduces in-memory scoring/weighting/aggregation/assessment
  IDs through SQL, including attack quarantine, recovery and unavailable output.
- `scripts/check_postgres.py` provisions the isolated cluster, runs either the five
  storage checks or the full suite, retains server logs/JUnit XML, and stops the
  server after success or failure. The final full-suite run confirmed shutdown.

PostgreSQL 17 in the pinned Compose/CI environment, actual Redis behavior, container
builds, host-failure durability, failover, sustained load and live-provider recovery
remain unverified locally. Docker's daemon is not running. This PostgreSQL 18.1
result does not establish upstream independence or enable publication. See
[commands, scope and limits](RUNTIME_VALIDATION.md).
