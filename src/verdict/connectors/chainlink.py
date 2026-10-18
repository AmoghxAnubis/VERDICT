import time
from datetime import UTC, datetime
from decimal import Decimal, localcontext

import httpx
from pydantic import BaseModel, ConfigDict

from verdict.connectors.common import check_clock, epoch_seconds
from verdict.connectors.ethereum import ETH_USD_FEED, ROUND_TYPES, EthereumRPC, decode_return
from verdict.models import Address, ChainBlock, Dataset, Observation, SourceProvenance

FEED_CALLS = {
    "decimals": "decimals()",
    "description": "description()",
    "round": "latestRoundData()",
}


class FeedBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: Address
    block: ChainBlock
    calls: dict[str, str]


def read_feed(bundle: FeedBundle, address: str, description: str) -> dict:
    if bundle.contract.lower() != address.lower():
        raise ValueError("unexpected Chainlink feed address")
    try:
        (decimals,) = decode_return(("uint8",), bundle.calls["decimals"])
        (actual_description,) = decode_return(("string",), bundle.calls["description"])
        round_id, answer, started_at, updated_at, answered_in_round = decode_return(
            ROUND_TYPES, bundle.calls["round"]
        )
    except KeyError as error:
        raise ValueError("incomplete Chainlink evidence") from error
    if actual_description != description or decimals != 8:
        raise ValueError("unexpected Chainlink instrument or decimals")
    timestamp = epoch_seconds(updated_at)
    if (
        round_id <= 0
        or answer <= 0
        or started_at <= 0
        or updated_at <= 0
        or started_at > updated_at
    ):
        raise ValueError("incomplete or invalid Chainlink round")
    if timestamp > bundle.block.timestamp:
        raise ValueError("Chainlink update is later than its block")
    with localcontext() as context:
        context.prec = 80
        value = Decimal(answer).scaleb(-decimals)
    return {"value": value, "timestamp": timestamp, "round_id": str(round_id)}


def normalize_feed(
    payload: dict, *, received_at: datetime, latency_ms: float, dataset: Dataset = "live"
) -> Observation:
    bundle = FeedBundle.model_validate(payload)
    feed = read_feed(bundle, ETH_USD_FEED, "ETH / USD")
    return Observation(
        dataset=dataset,
        source="chainlink",
        source_type="oracle",
        provider_event_id=f"1:{ETH_USD_FEED}:{feed['round_id']}",
        value=feed["value"],
        timestamp=feed["timestamp"],
        received_at=received_at,
        latency_ms=latency_ms,
        raw_payload=payload,
        provenance=SourceProvenance(
            protocol="chainlink",
            instrument="ETH/USD",
            verification="rpc_checked",
            dependencies=("chainlink_eth_usd", "configured_ethereum_rpc"),
            block=bundle.block,
            contract=ETH_USD_FEED,
        ),
    )


class ChainlinkConnector:
    def __init__(
        self,
        client: httpx.AsyncClient,
        max_future_skew_seconds: float = 5,
        *,
        rpc_url: str = "https://ethereum-rpc.publicnode.com",
        block_tag: str = "latest",
    ):
        self.rpc = EthereumRPC(client, rpc_url)
        self.max_future_skew_seconds = max_future_skew_seconds
        self.block_tag = block_tag

    async def fetch(self) -> Observation:
        started = time.perf_counter()
        block = await self.rpc.block(self.block_tag)
        calls = await self.rpc.calls(ETH_USD_FEED, FEED_CALLS, block)
        await self.rpc.ensure_canonical(block)
        observation = normalize_feed(
            {"contract": ETH_USD_FEED, "block": block.model_dump(mode="json"), "calls": calls},
            received_at=datetime.now(UTC),
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return check_clock(observation, self.max_future_skew_seconds)
