# VERDICT

An evidence and trust layer for off-chain data. The project starts with ETH/USD
observations, then adds explainable aggregation and testnet publication.

The backend provides a FastAPI service, decimal-safe observation schema, versioned
SQLite/PostgreSQL storage, three public exchange adapters, persisted connector
health, deterministic replay, and explainable statistical rounds.
Consensus, trust scores, the dashboard, and contracts are
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
```

The first command polls all three exchanges once; the second polls until Ctrl+C.
Each provider runs independently. A failed provider does not discard other
providers' observations. One-shot mode reports each result and exits unsuccessfully
if any provider fails. Continuous workers retry with capped exponential backoff
and respect `Retry-After` up to five minutes. The API never starts ingestion itself.

The connector uses Coinbase's public
[product ticker endpoint](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker).
It records the last trade, not a guaranteed executable quote. REST polling is the
initial adapter; streaming and REST recovery are later ingestion work.
Kraken uses its [recent ETH/USD trades endpoint](https://docs.kraken.com/api-reference/market-data/get-recent-trades).
Binance.US uses [recent ETHUSD trades](https://docs.binance.us/#get-recent-trades)
and is identified as `binance_us`, distinct from Binance's global USDT markets.
All three endpoints were reachable during local validation; availability and trade
freshness can differ. HTTP success does not imply a fresh underlying trade.

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
| `GET /v1/feed/ETH-USD` | HTTP 503 with null value/confidence until consensus is built |

Observation queries support `asset=ETH-USD`, `source=coinbase|kraken|binance_us`,
and `limit=1..1000`. Prices are JSON
strings to preserve decimal precision. `latency_ms` measures the HTTP request
duration, not the age of the provider's trade. A repeated trade is stored once;
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
processes; both use the same migrated database. Redis is deferred until the
ingestion queue is implemented; this slice writes directly to storage.

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

See [the MVP specification](docs/MVP_SPEC.md) for rules and assumptions, and
[the original vision](VERDICT_Project_Document.md) for the full project document.
