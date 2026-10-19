# Redis polling ingestion

`verdict ingest --transport redis` publishes normalized live polling attempts to
Redis. `verdict consume` persists them to SQLite or PostgreSQL. Direct ingestion
remains the default. All six polling adapters use the same queue envelope; replay
continues to write atomically into its isolated database namespace.

## Run locally

Install dependencies with `uv sync --frozen`, then run:

```powershell
docker compose --profile queue up -d redis
uv run --no-sync verdict db upgrade
uv run --no-sync verdict consume
```

In another terminal:

```powershell
uv run --no-sync verdict ingest --transport redis --source coinbase --once
uv run --no-sync verdict ingest --transport redis
```

One-shot producers report `status=queued`; this confirms Redis acceptance, before
database persistence. Producer processes do not open or require the database.
The API reads only committed database evidence, so a stopped consumer leaves API
observations unchanged and their freshness continues to expire normally.

`verdict consume --once` runs one bounded recovery scan and one new-message read,
each capped by `VERDICT_REDIS_BATCH_SIZE`. It does not wait for new messages or
guarantee that all pending entries have reached their reclaim deadline. Repeated
runs or continuous consumption handle the remaining work. Rejected entries cause
one-shot consumption to exit unsuccessfully.

`VERDICT_INGESTION_TRANSPORT=redis` sets the producer default; `--transport direct`
overrides it. Set `VERDICT_REDIS_URL` for authentication or remote Redis. The URL
is held as a secret setting and application retry logs contain error types only.
See `.env.example` for stream/group, batch size, reclaim idle time, and read timeout.
Use a different stream and group for each environment.

Coinbase WebSocket ingestion retains its direct, atomic evidence/checkpoint path.
Continuous `all`/`coinbase` ingestion rejects Redis transport if Coinbase streaming
is enabled. Disable `VERDICT_COINBASE_STREAM_ENABLED` for queued polling, or run
Coinbase separately with direct transport and the other sources with Redis. A
one-shot Coinbase poll remains supported when streaming is enabled in configuration.

## Delivery and recovery

Each version-1 envelope contains an attempt UUID, producer attempt timestamp,
source, and either one normalized live observation or a sanitized error class and
optional HTTP status. Prices remain decimal strings; original observation receipt
time, provenance, and provider JSON survive the transport. Replay observations,
source mismatches, unknown versions/fields, and malformed messages are rejected.

The group starts at stream ID `0-0`, including entries published before the first
consumer. Redis consumer groups track delivered entries until acknowledgement;
[`XAUTOCLAIM`](https://redis.io/docs/latest/commands/xautoclaim/) transfers stale
pending messages to a replacement consumer. This implementation requires Redis 7
or newer so removed pending payload IDs are also reported.

For every accepted entry, the database transaction commits all three effects:

1. A unique `ingestion_receipts` row keyed by attempt UUID and canonical content hash.
2. The first observation receipt, rejecting conflicting provider event content.
3. Connector health counters for that distinct attempt.

Only then does the consumer send `XACK`. If it stops after commit and before
acknowledgement, a replacement consumes the same attempt without changing evidence
or counters. Concurrent deliveries of the same attempt also have one database
effect. A different poll returning the same provider event counts as another
received attempt while leaving the first observation untouched. Reusing an attempt
UUID with different content is a conflict.

Health timestamps and consecutive failures describe the order in which the
consumer applies distinct attempts. They are not a producer heartbeat and can
lag during a backlog; reclaimed attempts can arrive out of production order.
Original producer times remain in the receipt ledger. Observation freshness and
statistical cutoffs continue to use provider event and original receipt times.

Continuous producers retry temporary Redis connection/timeouts with the same
envelope before fetching again. An uncertain `XADD` response can create multiple
stream entries, all deduplicated by the SQL receipt. One-shot queue failures fail
the command. A producer crash before Redis acceptance has no durable local spool;
polling does not promise recovery of all intermediate provider trades. Coinbase's
direct streaming mode remains the path with contiguous trade recovery.

Continuous consumers retry connection and database failures with capped backoff.
The reclaim scan cursor advances across batches; fresh messages are also read on
every pass. Each process generates a unique consumer name unless `--consumer` is
given. Configure the reclaim idle time above expected batch processing duration;
an early claim is safe for SQL effects but can cause redundant work.

## Rejections and retention

Malformed messages and conflicting attempts/observations stay unacknowledged in
the original stream. Their IDs and error types are logged without provider bodies.
They are retried after the idle deadline while other messages continue. Operators
can inspect them with `XPENDING` and `XRANGE` and resolve them explicitly after
retaining their evidence. There is no automatic discard or dead-letter expiration.

The application never trims the stream, removes consumers, or expires receipt
rows. Retain the receipt ledger for as long as messages can be redelivered.
Deleting pending payloads externally causes consumption to fail with
`QueueDataLoss`; recover the missing evidence before resuming. Do not use `MAXLEN`
or independent key eviction as an ingestion retention policy. Memory and retained
stream/ledger size require operational monitoring; automatic safe compaction is
not implemented.

Compose's optional queue profile uses a named Redis volume, AOF with
`appendfsync always`, and `noeviction`. Durability still depends on the Redis
deployment and storage. See the [Redis persistence documentation](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/).

To run containers after database migration, select the queue services explicitly:

```powershell
docker compose --profile queue build api collector consumer
docker compose up -d db
docker compose run --rm api verdict db upgrade
docker compose --profile queue up -d api redis collector consumer
```

Select either `worker` for direct ingestion or `collector`/`consumer` for queued
ingestion of the same sources. Avoid running both for one source: independent
collectors count as distinct polling attempts.

## Validation

Local tests use an in-memory Redis emulator for group reads, stale claims, missing
payloads, duplicate/ambiguous publish outcomes, and acknowledgement failure. Actual
SQLite migrations, transactions, reconnections, and concurrent receipt claims run
locally too. Set `VERDICT_TEST_REDIS_URL` to a disposable Redis 7+ service to run
the real Redis delivery/reclaim contract; it uses a unique stream/group and removes
only that stream afterward. CI provides Redis and PostgreSQL services. Local
emulation does not establish real Redis durability, load capacity, or failover
behavior.
