# VERDICT

An evidence and trust layer for off-chain data. The project starts with ETH/USD
observations, then adds explainable aggregation and testnet publication.

This first development batch provides a FastAPI service, decimal-safe observation
schema, versioned SQLite/PostgreSQL storage, a public Coinbase REST connector, and
deterministic replay. Consensus, trust scores, the dashboard, and contracts are
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
```

The first command polls once; the second polls until Ctrl+C. Network or validation
failures in continuous mode retry with capped exponential backoff. A one-shot
failure exits unsuccessfully. The API itself never starts ingestion implicitly.

The connector uses Coinbase's public
[product ticker endpoint](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker).
It records the last trade, not a guaranteed executable quote. REST polling is the
initial adapter; streaming and REST recovery are later ingestion work.

## API

| Route | Behavior |
| --- | --- |
| `GET /health/live` | Process health |
| `GET /health/ready` | Database/table readiness |
| `GET /v1/observations` | Latest stored live observations, newest event first |
| `GET /v1/observations?dataset=replay:baseline` | Explicitly selected replay observations |
| `GET /v1/sources` | Latest observation freshness, with trust unset |
| `GET /v1/feed/ETH-USD` | HTTP 503 with null value/confidence until consensus is built |

Observation queries support `asset=ETH-USD` and `limit=1..1000`. Prices are JSON
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
rows. The baseline includes a duplicate and an out-of-order arrival. The other
fixtures are inputs for the upcoming anomaly engine; they do not yet detect attacks.
Historical fixtures naturally show as stale in the current-time source endpoint.

The raw JSON object is retained, but not the original HTTP byte stream or headers.
SHA-256 here is a reproducibility checksum, not a provider signature or Merkle proof.

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
