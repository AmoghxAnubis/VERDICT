# Versioned dependency registry

The dependency registry records the relationships supported by retained evidence
and the gaps that remain unknown. It is the first Batch 3 foundation. It does not
assign trust scores, weights, confidence, or permission to publish.

The bundled `dependencies-v1` review covers all six ETH/USD adapters. Its scope is
the checked-in implementation, not an attestation of a running deployment or an
exhaustive account of provider internals. The review retains eight connector source
files and an explicit limitations note, each with a SHA-256 checksum. No external
provider membership or independence claim was added during this implementation.

## Register and inspect

Apply migration `0006`, then register the bundled review explicitly:

```powershell
uv run --no-sync verdict db upgrade
uv run --no-sync verdict dependencies register
```

Registration prints the immutable registry ID, version, and whether it inserted a
new row. Repeating it is idempotent. The bundled review's registry ID is:

```text
e6c74460fbff40dd4517bba9b715e47094b39b0cce0dfda44b9df7146820618b
```

Inspect the full graph, or explicitly select the sources under consideration:

```powershell
uv run --no-sync verdict dependencies inspect e6c74460fbff40dd4517bba9b715e47094b39b0cce0dfda44b9df7146820618b
uv run --no-sync verdict dependencies inspect e6c74460fbff40dd4517bba9b715e47094b39b0cce0dfda44b9df7146820618b --source chainlink --source uniswap_v3
```

A separately reviewed JSON registry can be imported with
`verdict dependencies register --file path/to/registry.json`. The input shape is
the bundled [registry file](../src/verdict/data/dependencies-v1.json), without the
API envelope's `registry_id`. Registration validates structural consistency and
evidence hashes; it cannot establish that the author's statements are true.
Only import reviewed, credential-free evidence. Retained evidence is exposed by
the read API, and references are descriptive strings that are never fetched.

| Read-only route | Result |
| --- | --- |
| `GET /v1/dependencies` | Retained version summaries, newest review first; `limit=1..1000` |
| `GET /v1/dependencies/{registry_id}` | Exact graph and retained evidence with its hash |
| `GET /v1/dependencies/{registry_id}/inspection` | Deterministic dependency closure and shared dependencies |

Inspection accepts repeated `source` query parameters, for example
`?source=chainlink&source=uniswap_v3`. Omitting them selects all six sources.
Unknown or duplicate source selections fail validation. A missing registry returns
404. API reads never seed the database, register reviews, or modify observations.

## What the initial graph establishes

| Relationship | Recorded interpretation |
| --- | --- |
| Coinbase, Kraken, Binance.US | Separate direct USD trade endpoints; participant and cross-venue economic relationships remain unknown |
| Chainlink ETH/USD and Uniswap's Chainlink USDC/USD conversion | Distinct feeds with a shared protocol family; identical publishers or operators are **not** established |
| Chainlink and Uniswap | Shared Ethereum mainnet dependency and the same configured RPC slot in `build_connector` |
| Uniswap | Pool spot price, USDC quote conversion, and the adapter's explicit WETH redemption assumption |
| Pyth | Parsed ETH/USD data delivered through configured Hermes; publisher membership and overlap remain unknown |
| All six adapters | Common VERDICT collector deployment; host/network and service operator details remain unknown |

Each edge is classified as `price`, `delivery`, `settlement`, `conversion`,
`protocol`, or `unknown`. Node types distinguish markets, feeds, protocols,
transport, chains, assets, and unknowns. This preserves the distinction between an
availability dependency and an economic price dependency for later policy work.

Inspection follows dependencies transitively. For example, the Uniswap path
reaches its USDC/USD feed, that feed's protocol family, and its unknown upstream
membership. Shared known nodes list affected selected sources. Unknown placeholders
are listed separately; sharing one does not establish actual shared upstreams,
and having different placeholders does not establish independence.

All six sources in the initial review have unresolved dependencies. Every
inspection returns `independent_domain_count=null` and `publication_ready=false`,
even for a future graph with no declared unknown nodes. Absence of a recorded edge
is not evidence of independence. Prices and correlations never create graph edges.

## Reproducibility and version changes

The registry ID hashes the complete validated document: schema, human-readable
version, review time, scope, source mappings, nodes, edges, and retained evidence.
Collections and evidence references use canonical ordering; timestamps normalize
to UTC. The encoding is UTF-8 JSON with sorted keys, compact separators, and ASCII
escaping. Evidence content is hashed exactly as retained. The bundled source
snapshots use LF newlines. These checksums support reproducibility, not authenticity.

The graph must cover each provider exactly once with a distinct source node. It
rejects cycles, missing references, duplicate identities/edges, unreferenced
evidence, unreachable nodes, empty source dependency declarations, and evidence
captured after review. Unknowns must be terminal and have explicitly unknown
incoming relationships. Inputs are bounded to 256 nodes, 1,024 edges, and 64
evidence items with at most 100,000 characters each.

Storage retains canonical payloads with unique registry hashes and version names.
Neither registration nor the API provides an update/delete path. Changed content
under an existing version is rejected; a reviewed change requires a new version.
Reads revalidate the document and its hash. This is application-level immutability,
not protection from an administrator rewriting the database.

The inspection ID commits to `dependency-inspection-v1`, the registry hash, the
selected source set, and the derived result. Adding a newer registry does not
change an inspection pinned to an earlier one. There is deliberately no implicit
"current graph" selection. `reviewed_at` records the review, not a historical
effective interval or proof that information was known at an observation cutoff.

Existing statistical rounds and the frozen historical evaluation remain the
original statistical artifacts. They do not acquire dependency-based weights or
retroactively pin this review. The forthcoming trust/consensus round schema must
retain an explicitly selected registry hash alongside the statistical policy and
inputs, and separately address observation/deployment matching and review validity.

## Remaining work

Feed-specific membership evidence, independently supported upstream domains,
deployment identities, evidence freshness/review policy, source scoring,
dependency-aware weight caps, quarantine/recovery, and consensus remain pending.
The shared collector is an operational failure domain; it is not automatically an
economic voting domain. The weighting policy must state how it uses each edge type.

Historical coverage and calibration gaps remain open. Registering this review
does not satisfy those gates; `/v1/feed/ETH-USD` still returns 503.
