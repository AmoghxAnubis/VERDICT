# VERDICT

An evidence and trust layer for off-chain data. The project starts with ETH/USD
observations, then adds explainable aggregation and testnet publication.

The backend provides a FastAPI service, decimal-safe observation schema, versioned
SQLite/PostgreSQL storage, three exchange adapters, Chainlink/Pyth oracle reads,
a Uniswap v3 pool adapter, optional Redis polling ingestion, persisted connector
health, deterministic replay, explainable statistical rounds, a versioned dependency
registry, diagnostic source scores with retained evidence and cold-start rules,
and persistent quarantine/recovery with dependency-constrained weights and
diagnostic weighted-median candidates, and retained quality/availability assessments.
The Next.js dashboard adds source details, history, alerts, dependency inspection,
isolated replay controls, and resumable round updates. See
[dashboard setup and browser checks](docs/DASHBOARD.md).
Retained rounds can also be exported as self-contained evidence bundles with
offline verification and Merkle inclusion proofs; see [evidence commands](docs/EVIDENCE.md).
An [offline signing workflow](docs/SIGNING.md) adds EIP-712 diagnostic reports,
encrypted development keys, and public signer rotation/revocation registries.
Local [oracle/consumer contracts](docs/CONTRACTS.md) verify the same signatures,
enforce authorization/expiry/replay protection, and keep unusable output withheld.
The [Sepolia preparation tools](docs/TESTNET_RUNBOOK.md) add pinned deployment plans,
read-only preflight, and receipt audits; public wallet deployment remains pending.
The [repeatable local demonstration](docs/TESTNET_DEMO.md) retains manipulation,
outage, and recovery evidence through signatures, contract execution, and audited receipts.
Independent-domain evidence, usable feed output, and public deployment are
tracked in [the roadmap](docs/ROADMAP.md). The feed endpoint returns an explicit
unavailable response until consensus exists.

## Local setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/). Run commands
from the repository root. Python 3.12 is selected by `.python-version`.

PowerShell (all runtime and cache files stay in this checkout):

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD '.cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD '.python'
uv python install 3.12 --no-bin
uv sync --frozen
uv run --no-sync verdict db upgrade
uv run --no-sync verdict replay simulations/baseline.json
uv run --no-sync verdict serve
```

On macOS/Linux, use `export UV_CACHE_DIR="$PWD/.cache/uv"` and
`export UV_PYTHON_INSTALL_DIR="$PWD/.python"` instead of the two PowerShell lines.

Open <http://127.0.0.1:8000/docs> for the API explorer. SQLite uses
`data/verdict.db`. Copy `.env.example` to `.env` for overrides. Migrations are an
explicit setup step; starting the API does not change the schema.

In another terminal, after setting the same uv environment variables:

```powershell
uv run --no-sync verdict ingest --once
uv run --no-sync verdict ingest
uv run --no-sync verdict ingest --source kraken --once
uv run --no-sync verdict ingest --source chainlink --once
uv run --no-sync verdict ingest --source pyth --once
uv run --no-sync verdict ingest --source uniswap_v3 --once
```

The first command polls all six providers once; the second polls until Ctrl+C.
Each provider runs independently. A failed provider does not discard other
providers' observations. One-shot mode reports each result and exits unsuccessfully
if any provider fails. Continuous workers retry with capped exponential backoff
and respect `Retry-After` up to five minutes. The API never starts ingestion itself.

The REST connector uses Coinbase's public
[product ticker endpoint](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker).
It records the last trade, not a guaranteed executable quote. Continuous Coinbase
streaming with durable checkpoints and REST gap recovery is also available:

```powershell
uv run --no-sync verdict db upgrade
uv run --no-sync verdict ingest --source coinbase --coinbase-stream
# Stream Coinbase while the other five providers continue independent polling:
uv run --no-sync verdict ingest --coinbase-stream
```

The default remains REST polling. `VERDICT_COINBASE_STREAM_ENABLED=true` enables
streaming for continuous ingestion through configuration; `--once` still polls
REST. The explicit `--coinbase-stream` flag cannot be combined with `--once` or
another individual source. First launch establishes a baseline at the latest REST
trade; reconnects and heartbeat gaps recover forward from that retained checkpoint.
Incomplete recovery does not advance the checkpoint. `/v1/sources` exposes live
stream status and heartbeat receipt alongside ordinary freshness and health.
See [streaming behavior and limits](docs/COINBASE_STREAMING.md).

Kraken uses its [recent ETH/USD trades endpoint](https://docs.kraken.com/api-reference/market-data/get-recent-trades).
Binance.US uses [recent ETHUSD trades](https://docs.binance.us/#get-recent-trades)
and is identified as `binance_us`, distinct from Binance's global USDT markets.
All three endpoints were reachable during local validation; availability and trade
freshness can differ. HTTP success does not imply a fresh underlying trade.

Chainlink and Uniswap use read-only Ethereum mainnet JSON-RPC requests pinned to
one canonical block hash. Configure `VERDICT_ETHEREUM_RPC_URL` with a provider
supporting EIP-1898; `VERDICT_ETHEREUM_BLOCK_TAG` selects `latest`, `safe`, or
`finalized`. On-chain workers poll every 15 seconds by default. Uniswap reads the
USDC/WETH 0.05% v3 pool and explicitly converts USDC to USD with a same-block
Chainlink feed. Pyth reads Hermes ETH/USD and retains its update bytes as
unverified evidence. Set `VERDICT_PYTH_API_KEY` in your environment or local `.env`
for authenticated Hermes access. `VERDICT_PYTH_HERMES_URL` overrides the service.
Missing credentials/provider failures are reported independently; one-shot `all`
exits unsuccessfully if any source fails. See [adapter details](docs/ORACLE_ADAPTERS.md)
for evidence boundaries and operating limits.

## API

| Route | Behavior |
| --- | --- |
| `GET /health/live` | Process health |
| `GET /health/ready` | Database/table readiness |
| `GET /v1/observations` | Latest stored live observations, newest event first |
| `GET /v1/observations?dataset=replay:baseline` | Explicitly selected replay observations |
| `GET /v1/sources` | Per-provider freshness and persisted connectivity/error counters |
| `GET /v1/analysis/ETH-USD` | Read-only statistical diagnostics for a fixed cutoff |
| `GET /v1/rounds` | Statistical rounds explicitly saved by the CLI |
| `GET /v1/rounds/{round_id}` | One saved round, scoped to the selected dataset |
| `GET /v1/scores` | Saved diagnostic source scores, scoped to the selected dataset |
| `GET /v1/scores/{report_id}` | Score breakdowns, sampled decisions, and pinned evidence |
| `GET /v1/weights` | Saved quarantine/recovery transitions and constrained weights |
| `GET /v1/weights/{report_id}` | Source states, dependency share caps, and pinned score/history |
| `GET /v1/aggregations` | Saved diagnostic weighted-median candidates |
| `GET /v1/aggregations/{report_id}` | Pinned observations, weights, exclusions, and candidate |
| `GET /v1/confidence` | Saved diagnostic quality and availability assessments |
| `GET /v1/confidence/{report_id}` | Quality components, dependency exposures, and gate decisions |
| `GET /v1/dependencies` | Retained dependency registry versions |
| `GET /v1/dependencies/{registry_id}` | Pinned graph and retained evidence |
| `GET /v1/dependencies/{registry_id}/inspection` | Transitive dependencies, overlaps, and unknowns |
| `GET /v1/feed/ETH-USD` | HTTP 503 with null value/confidence until consensus is built |

Observation queries support `asset=ETH-USD`,
`source=coinbase|kraken|binance_us|chainlink|pyth|uniswap_v3`,
and `limit=1..1000`. Prices are JSON
strings to preserve decimal precision. For HTTP observations, `latency_ms` measures
request duration, not trade age. Stream matches use zero to indicate unmeasured
transport latency; receipt time records when the application consumes the message.
A repeated trade is stored once;
it does not become fresh just because polling succeeds again. Freshness uses the
provider timestamp, not the receipt timestamp.

## Replay

```powershell
uv run --no-sync verdict replay simulations/baseline.json
uv run --no-sync verdict replay simulations/extreme-price.json
uv run --no-sync verdict replay simulations/stale-feed.json
```

Replay never contacts providers and always uses a `replay:<scenario>` namespace.
Running the same fixture twice gives the same normalized SHA-256 and no additional
rows. The baseline includes a duplicate and an out-of-order arrival. The original
single-source fixtures cannot establish cross-source agreement. Multi-provider
fixtures below exercise the statistical engine.
Historical fixtures naturally show as stale in the current-time source endpoint.

`six-provider.json` adds synthetic ABI evidence for Chainlink and Uniswap plus a
Pyth Hermes response. Replay it and analyze at `2026-10-16T00:00:10Z` to exercise
all six sources, source provenance, and explicit USD conversion. It is synthetic
test data; the Pyth binary is a placeholder and is not a verified update.

The provider payload is retained, but not the original HTTP byte stream or headers.
Kraken fractional epoch numbers are decoded as strings to preserve their lexical
precision. Binance.US responses are wrapped with the adapter-pinned `ETHUSD`
instrument and the unmodified trade array.
SHA-256 here is a reproducibility checksum, not a provider signature or Merkle proof.

## Statistical rounds

```powershell
uv run --no-sync verdict replay simulations/single-outlier.json
uv run --no-sync verdict analyze --dataset replay:single-outlier --at 2026-10-16T00:00:10Z
```

The report flags the extreme Binance.US input, records median/MAD and threshold,
and reports insufficient remaining sources. `market-baseline.json` demonstrates
healthy agreement; `market-jump.json` demonstrates a coordinated move; use the same
cutoff. `provider-outage.json` uses `--at 2026-10-16T00:00:40Z`.

`verdict analyze` saves an immutable diagnostic round. For live analysis, omit
`--dataset` and `--at`; `--window 300` controls history. Replay requires an explicit
cutoff. The API equivalent is:

```text
GET /v1/analysis/ETH-USD?dataset=replay:single-outlier&at=2026-10-16T00:00:10Z
GET /v1/rounds?dataset=replay:single-outlier
```

GET analysis does not persist a round. Reports contain policy/input IDs and a
deterministic round ID. All diagnostic reports have `publication_ready=false`.
See [the statistical policy](docs/STATISTICAL_ENGINE.md) for formulas, assumptions,
and fixture acceptance criteria.

## Dependency evidence

```powershell
uv run --no-sync verdict db upgrade
uv run --no-sync verdict dependencies register
# Use the registry_id returned by registration:
uv run --no-sync verdict dependencies inspect <registry_id> --source chainlink --source uniswap_v3
```

The bundled review records relationships supported by the connector implementation,
including Uniswap's Chainlink USD conversion and shared Ethereum/RPC dependencies.
Unknown upstream memberships remain explicit. Inspections are pinned to a retained
version and do not establish source independence or assign voting weights. See
[registry evidence, API access, and limits](docs/DEPENDENCIES.md).

## Diagnostic source scores

```powershell
uv run --no-sync verdict score --registry-id <registry_id> --dataset replay:six-provider --at 2026-10-16T00:00:10Z
```

After migration, registry registration, and replay, this saves fixed-grid availability,
freshness, and peer-consistency scores with explicit cold-start requirements. The
short fixture demonstrates cold start. Scores retain every sampled decision and
their input/registry/policy hashes. They are development quality indicators with
publication disabled. See [scoring rules, CLI options, and limits](docs/SOURCE_SCORING.md).

## Quarantine and weights

Apply a retained scoring report to its dataset's persistent weighting history:

```powershell
uv run --no-sync verdict weight <scoring_report_id> --dataset replay:six-provider
```

Failures remove weight immediately. Recovery requires new evidence and elapsed
time, followed by gradual weight increases. Caps limit each source, shared economic
dependencies, and the pooled unresolved upstreams as shares of the final total.
The bundled registry has unknowns for all six sources, so its conservative cap
permits zero total weight even after recovery. Repeating a score is idempotent;
new scores must advance in cutoff order with the same registry and policies.
This does not yet produce consensus or enable publication. See
[state transitions, policy defaults, and storage](docs/WEIGHTING.md).

## Weighted-median candidates

```powershell
uv run --no-sync verdict aggregate <weighting_report_id> --dataset replay:six-provider
```

Aggregation uses the weighting report's exact scoring evidence and final weights.
Exact half-weight ties select the lower price. Zero total weight returns an
unavailable report with a null candidate; the bundled registry's unresolved
upstreams therefore cannot produce a candidate. Reports retain all source samples,
weights, and exclusions. Quality/availability assessments consume these reports
separately; the live feed remains unavailable. See
[aggregation rules and evidence](docs/AGGREGATION.md).

## Quality and availability

```powershell
uv run --no-sync verdict confidence <aggregation_report_id> --dataset replay:six-provider
```

Assessments publish an uncalibrated quality score with component contributions
and gates for source count, allocated budget, quality, concentration, unknowns,
registry age, and independent domains. A high quality score cannot override missing
independence evidence. Registry v1 cannot establish independent domains, so all
assessments keep usable value/confidence null and publication disabled. See
[formulas, defaults, and evidence requirements](docs/CONFIDENCE.md).

## Historical evaluation

Retained public ETH/USD trade windows support offline comparison of the frozen
statistical policy, with separate calibration/holdout periods and explicit
spike, coordinated-move, correlated-shift, outage, and late-receipt interventions:

```powershell
uv run --no-sync verdict history evaluate benchmarks/historical-v1.json benchmarks/captures --output .cache/historical-report.json
```

Historical receipt times are assumed, gaps remain missing, and the result does not
enable publication. See [methods and results](docs/HISTORICAL_VALIDATION.md).

## Full-pipeline adversarial benchmarks

```powershell
uv run --no-sync verdict benchmark benchmarks/adversarial-v1.json --output .cache/adversarial-report.json
```

The offline synthetic suite exercises scoring, quarantine/recovery, weights,
aggregation and availability gates across 400 rounds. The correlated-majority
attack target fails; all usable outputs remain unavailable. `--require-targets`
writes the report before returning a failing exit code. See
[methods and results](docs/ADVERSARIAL_BENCHMARKS.md) and the
[delivery estimate](docs/DELIVERY_ESTIMATE.md).

## PostgreSQL and Docker

```powershell
docker compose up -d db
$env:VERDICT_DATABASE_URL = 'postgresql+psycopg://verdict:verdict@localhost:5432/verdict'
uv run --no-sync verdict db upgrade
uv run --no-sync verdict replay simulations/baseline.json
```

Or run the API and ingestion worker in containers:

```powershell
docker compose build api worker
docker compose run --rm api verdict db upgrade
docker compose up -d api worker
```

Compose credentials are local development defaults. The database port and API bind
to localhost. Data persists in a named volume. API and worker remain separate
processes; both use the same migrated database. Direct ingestion writes to storage.
An optional Redis queue separates polling collectors from database consumers:

```powershell
docker compose --profile queue up -d redis
uv run --no-sync verdict db upgrade
uv run --no-sync verdict consume
# In another terminal:
uv run --no-sync verdict ingest --transport redis
```

All six polling adapters support this transport. Producers report queued attempts;
consumers commit evidence and health before acknowledging Redis, with durable
deduplication and pending-message recovery. Coinbase WebSocket ingestion continues
to use its direct checkpoint path. See [queue operation and limits](docs/REDIS_INGESTION.md)
for configuration, container startup, rejection handling, and retention.

## Validation

```powershell
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest
```

Tests use temporary SQLite files and mocked provider responses. To run the same
storage contract against a disposable PostgreSQL database, set
`VERDICT_TEST_DATABASE_URL` before pytest. The PostgreSQL test applies migrations
and uses an isolated replay namespace; do not point it at production.

With PostgreSQL installed, create, test, and stop an isolated local cluster:

```powershell
.venv/Scripts/python.exe scripts/check_postgres.py --postgres-bin 'C:/Program Files/PostgreSQL/18/bin' --full-suite
```

See [runtime validation and measured coverage](docs/RUNTIME_VALIDATION.md).

See [the MVP specification](docs/MVP_SPEC.md) for rules and assumptions, and
[the original vision](VERDICT_Project_Document.md) for the full project document. 
