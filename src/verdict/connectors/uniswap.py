import asyncio
import time
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

import httpx
from pydantic import BaseModel, ConfigDict, Field

from verdict.connectors.chainlink import FEED_CALLS, FeedBundle, read_feed
from verdict.connectors.common import check_clock
from verdict.connectors.ethereum import (
    SLOT0_TYPES,
    UNISWAP_FACTORY,
    USDC,
    USDC_USD_FEED,
    WETH,
    EthereumRPC,
    call_data,
    decode_return,
)
from verdict.models import (
    Address,
    ChainBlock,
    Dataset,
    Observation,
    QuoteConversion,
    SourceProvenance,
)

POOL_FEE = 500
POOL_CALLS = {
    "token0": "token0()",
    "token1": "token1()",
    "slot0": "slot0()",
    "liquidity": "liquidity()",
}


class PoolBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    factory: Address
    factory_pool_result: str
    pool: Address
    block: ChainBlock
    calls: dict[str, str]
    token_decimals: dict[str, str]
    conversion: FeedBundle
    conversion_max_age_seconds: int = Field(ge=1, le=172800, strict=True)


def normalize_pool(
    payload: dict, *, received_at: datetime, latency_ms: float, dataset: Dataset = "live"
) -> Observation:
    bundle = PoolBundle.model_validate(payload)
    if bundle.factory.lower() != UNISWAP_FACTORY:
        raise ValueError("unexpected Uniswap factory")
    (pool,) = decode_return(("address",), bundle.factory_pool_result)
    if pool.lower() != bundle.pool.lower() or int(pool, 16) == 0:
        raise ValueError("pool does not match the factory result")
    if bundle.conversion.block != bundle.block:
        raise ValueError("pool and USD conversion must use the same block")
    try:
        (token0,) = decode_return(("address",), bundle.calls["token0"])
        (token1,) = decode_return(("address",), bundle.calls["token1"])
        (decimals0,) = decode_return(("uint8",), bundle.token_decimals[USDC])
        (decimals1,) = decode_return(("uint8",), bundle.token_decimals[WETH])
        slot = decode_return(SLOT0_TYPES, bundle.calls["slot0"])
        (liquidity,) = decode_return(("uint128",), bundle.calls["liquidity"])
    except KeyError as error:
        raise ValueError("incomplete Uniswap evidence") from error
    if (token0.lower(), token1.lower(), decimals0, decimals1) != (USDC, WETH, 6, 18):
        raise ValueError("unexpected Uniswap pair or token decimals")
    sqrt_price = slot[0]
    if sqrt_price <= 0 or liquidity <= 0 or slot[-1] is not True:
        raise ValueError("uninitialized, empty, or locked Uniswap pool")
    conversion = read_feed(bundle.conversion, USDC_USD_FEED, "USDC / USD")
    age = (bundle.block.timestamp - conversion["timestamp"]).total_seconds()
    if age > bundle.conversion_max_age_seconds:
        raise ValueError("USD conversion feed is stale")
    with localcontext() as context:
        context.prec = 100
        # token1/token0 is raw WETH per raw USDC; invert and adjust 18/6 decimals.
        usdc_per_weth = Decimal(2**192) * Decimal(10**12) / Decimal(sqrt_price**2)
        value = (usdc_per_weth * conversion["value"]).quantize(
            Decimal("0.000000000000000001"), rounding=ROUND_HALF_EVEN
        )
    return Observation(
        dataset=dataset,
        source="uniswap_v3",
        source_type="dex",
        provider_event_id=f"1:{pool.lower()}:{bundle.block.hash.lower()}",
        event_sequence=bundle.block.number,
        value=value,
        timestamp=bundle.block.timestamp,
        received_at=received_at,
        latency_ms=latency_ms,
        raw_payload=payload,
        provenance=SourceProvenance(
            protocol="uniswap_v3",
            instrument="WETH/USDC fee=500 -> ETH/USD",
            verification="rpc_checked",
            block=bundle.block,
            contract=bundle.pool,
            dependencies=(
                "uniswap_v3_pool",
                "weth_redemption",
                "usdc",
                "chainlink_usdc_usd",
                "configured_ethereum_rpc",
            ),
            native_wrapping_ratio="1:1 WETH redemption",
            pool_liquidity_raw=str(liquidity),
            quote_conversion=QuoteConversion(
                feed_address=USDC_USD_FEED,
                round_id=conversion["round_id"],
                value=conversion["value"],
                updated_at=conversion["timestamp"],
                maximum_age_seconds=bundle.conversion_max_age_seconds,
            ),
        ),
    )


class UniswapConnector:
    def __init__(
        self,
        client: httpx.AsyncClient,
        max_future_skew_seconds: float = 5,
        *,
        rpc_url: str = "https://ethereum-rpc.publicnode.com",
        block_tag: str = "latest",
        conversion_max_age_seconds: int = 86400,
    ):
        self.rpc = EthereumRPC(client, rpc_url)
        self.max_future_skew_seconds = max_future_skew_seconds
        self.block_tag = block_tag
        self.conversion_max_age_seconds = conversion_max_age_seconds

    async def fetch(self) -> Observation:
        started = time.perf_counter()
        block = await self.rpc.block(self.block_tag)
        pool_result = await self.rpc.call(
            UNISWAP_FACTORY,
            call_data(
                "getPool(address,address,uint24)",
                ("address", "address", "uint24"),
                (USDC, WETH, POOL_FEE),
            ),
            block,
        )
        (pool,) = decode_return(("address",), pool_result)
        if int(pool, 16) == 0:
            raise ValueError("configured Uniswap pool does not exist")
        calls, usdc_decimals, weth_decimals, conversion_calls = await asyncio.gather(
            self.rpc.calls(pool, POOL_CALLS, block),
            self.rpc.call(USDC, call_data("decimals()"), block),
            self.rpc.call(WETH, call_data("decimals()"), block),
            self.rpc.calls(USDC_USD_FEED, FEED_CALLS, block),
        )
        await self.rpc.ensure_canonical(block)
        block_payload = block.model_dump(mode="json")
        observation = normalize_pool(
            {
                "factory": UNISWAP_FACTORY,
                "factory_pool_result": pool_result,
                "pool": pool,
                "block": block_payload,
                "calls": calls,
                "token_decimals": {USDC: usdc_decimals, WETH: weth_decimals},
                "conversion": {
                    "contract": USDC_USD_FEED,
                    "block": block_payload,
                    "calls": conversion_calls,
                },
                "conversion_max_age_seconds": self.conversion_max_age_seconds,
            },
            received_at=datetime.now(UTC),
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return check_clock(observation, self.max_future_skew_seconds)
