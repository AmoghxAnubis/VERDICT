# PostgreSQL runtime validation

The project can validate its PostgreSQL contract without a running Docker daemon.
An installed PostgreSQL distribution is sufficient:

```powershell
.venv/Scripts/python.exe scripts/check_postgres.py --postgres-bin 'C:/Program Files/PostgreSQL/18/bin'
.venv/Scripts/python.exe scripts/check_postgres.py --postgres-bin 'C:/Program Files/PostgreSQL/18/bin' --full-suite
```

The helper creates a new cluster and database under an ignored, unique
`.cache/postgres-runtime/run-*` directory. It binds only to `127.0.0.1` on an
automatically selected port, uses a dedicated test role, and passes its URLs only
to child processes. It does not connect to an existing PostgreSQL service.
Authentication trusts local test connections; the cluster contains generated test
data. Normal startup, test failures, and startup timeouts all attempt to stop the
owned server. Logs, database files, and `results.xml` remain in the run directory
for inspection. No database directories are automatically deleted.

On Windows, PostgreSQL's process-token handling may require execution outside the
sandbox. PostgreSQL tooling must run as a user permitted to initialize a cluster.
If tools are already on `PATH`, `--postgres-bin` can be omitted. `--full-suite`
enables the PostgreSQL tests alongside the ordinary SQLite/mocked tests; Redis
remains disabled because this runner does not provision Redis.

## Measured coverage

Validated on 24 October 2026 using installed PostgreSQL **18.1** on Windows.
Five actual PostgreSQL integration checks pass:

- All migrations through `0010`, ORM/schema agreement, decimal observation storage,
  retained registries and reports, concurrent idempotent report saves, weighting
  successors, queue receipts, health, and Coinbase checkpoints.
- Four concurrent consumers of one successful or failed polling attempt apply it
  once. Redelivery through a reopened connection leaves health unchanged. A new
  polling attempt still counts even when its observation is already retained.
- Changed attempt content is rejected. A conflicting observation rolls back the
  receipt and health effects; correcting and redelivering that attempt succeeds.
- A full 25-round, six-provider synthetic pipeline reproduces its in-memory report
  IDs through actual SQL selection and persistence, including nonzero allocation,
  isolated-attack quarantine, recovery, and null usable confidence/value.

The full regression run with PostgreSQL enabled passes **466 tests**, with the real
Redis test as the sole skip. Ruff lint and formatting checks pass. The helper
confirmed server shutdown after the full run.

## Fixed runtime defect

The first real run failed queue receipt deduplication: after a duplicate
`INSERT ... ON CONFLICT DO NOTHING`, psycopg exposed an unknown row count (`-1`).
Treating this as a successful claim reapplied the health update. Receipt claims now
use `RETURNING id`: an inserted ID permits effects; no returned ID identifies a
duplicate. This works for both PostgreSQL and SQLite, and preserves the surrounding
receipt/evidence/health transaction.

## Remaining verification

The Compose/CI configuration targets PostgreSQL **17**, while this local run uses
18.1. The Docker daemon is unavailable locally; container builds and PostgreSQL 17
runtime remain unverified here. Real Redis delivery/reclaim, persistence under host
failure, failover, load, and sustained live-provider operation are separate gates.
These checks do not establish upstream independence or enable publication.
