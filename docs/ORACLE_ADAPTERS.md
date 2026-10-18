# Oracle and DEX observation adapters

The six-source ingestion roster includes three direct USD exchanges, two oracle
networks, and one explicitly converted Uniswap pool. These adapters supply evidence
to statistical diagnostics. Consensus, dependency weighting, confidence, and feed
publication remain later milestones.

## Ethereum reads

Chainlink and Uniswap require Ethereum mainnet (chain ID 1). Each fetch selects
one block using the configured `latest`, `safe`, or `finalized` tag. Every contract
read uses [EIP-1898](https://eips.ethereum.org/EIPS/eip-1898) with that block hash
and `requireCanonical=true`; an additional block-number lookup rejects a changed
hash before normalization. RPC services without this support fail explicitly.
There are no wallet keys, transactions, gas payments, or contract writes.

Raw evidence retains the contract call results, chain ID, block number, hash,
timestamp, and requested block tag. `rpc_checked` means the configured RPC returned
consistent data; it is not a verified execution/state proof. A tag records the
RPC's claim at fetch time. `latest` is not final and can reorganize later; retained
observations are not automatically retracted after a later reorg.

`VERDICT_ONCHAIN_POLL_INTERVAL_SECONDS` defaults to 15 seconds and also sets the
minimum retry delay for Chainlink/Uniswap. Exchanges and Pyth use the ordinary
poll interval. Rate-limit responses retain the existing bounded Retry-After behavior.

## Chainlink ETH/USD

The mainnet proxy is `0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419`.
The adapter uses the [AggregatorV3 interface](https://docs.chain.link/data-feeds/api-reference),
checks `ETH / USD` and eight decimals, and rejects invalid/incomplete rounds or an
update timestamp later than the selected block. Event time is `updatedAt`, not the
block time. Event identity includes chain, proxy, and round. Repeated reads of the
same round preserve the first receipt and evidence, even if fetched at later blocks.

The global 30-second freshness policy can exclude a healthy but older Chainlink
update. Poll success does not refresh its event time. Cadence-specific calibration
is still required before publication decisions.

## Pyth Hermes ETH/USD

The pinned Core feed ID is
`ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace`.
The adapter requests parsed data and hex update messages through Hermes's
`/v2/updates/price/latest` endpoint. It checks feed identity, integer wire ranges,
exponent, publish time, slot, and binary hex encoding; prices and the provider's
confidence amount are normalized with Decimal arithmetic.

[Hermes requires authentication](https://docs.pyth.network/price-feeds/core/upgrade/preparing).
Set `VERDICT_PYTH_API_KEY`; it is sent as a Bearer header only on Hermes requests,
and is excluded from settings representations, retained evidence, and health
records. `VERDICT_PYTH_HERMES_URL` can select a different compatible service.
Authentication failures are ordinary isolated connector failures.

`hermes_unverified` is deliberate: binary messages are retained but signatures and
their binding to parsed fields are not verified here. The provider confidence
amount is not VERDICT confidence. Event identity includes publish time and slot;
higher slots win when multiple updates share a timestamp. No EVM finality is
claimed for a Hermes observation.

## Uniswap v3 USDC/WETH

The adapter resolves the 500-fee (0.05%) USDC/WETH pool through mainnet factory
`0x1f98431c8ad98523631ae4a59f267346ea31f984`, then checks token addresses,
6/18 decimals, nonzero active liquidity, and an initialized/unlocked pool.
It reads [slot0](https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolState.sol)
and computes:

```text
USDC per WETH = 2^192 * 10^12 / sqrtPriceX96^2
ETH/USD = USDC per WETH * Chainlink USDC/USD
```

USDC/USD uses proxy `0x8fffffd4afb6115b954bd326cbe7b4ba576818f6` at the exact
same block, with instrument/decimals/round checks and a configurable maximum age
(default 86,400 seconds). Conversion evidence retains its actual value, round,
update time, and age policy. WETH's 1:1 native redemption assumption is explicit.
Final normalized price rounds to 18 decimal places using half-even rounding.

This is pool spot, not a time-weighted oracle or an executable quote. Positive
liquidity alone does not establish manipulation resistance. Dependency labels
include USDC, WETH redemption, Chainlink conversion, and the configured RPC.

## Storage, replay, and limits

Run `verdict db upgrade` before using the new code. Migration 0003 adds indexed
provider sequence ordering and backfills sequences from existing JSON evidence
without rewriting payloads. SQLite upgrades and schema agreement are tested;
PostgreSQL runtime validation requires the optional test database.

`simulations/six-provider.json` is entirely synthetic, including placeholder Pyth
update bytes. It tests normalization, isolated/idempotent replay, retained
provenance, API serialization, and six-inlier diagnostics at
`2026-10-16T00:00:10Z`. It establishes behavior, not real-world calibration or six
independent domains. Live availability and held-out historical validation are
separate checks; `publication_ready` remains false and the feed returns HTTP 503.
