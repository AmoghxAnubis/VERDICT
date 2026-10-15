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
