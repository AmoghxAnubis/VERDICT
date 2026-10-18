"""Read-only Ethereum JSON-RPC and strict ABI decoding. No wallet or transactions."""

import asyncio
import itertools
import re
from datetime import UTC, datetime
from typing import Literal

import httpx
from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError, EncodingError
from eth_utils import keccak

from verdict.connectors.common import epoch_seconds
from verdict.models import ChainBlock

ETH_USD_FEED = "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419"
USDC_USD_FEED = "0x8fffffd4afb6115b954bd326cbe7b4ba576818f6"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
UNISWAP_FACTORY = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
ROUND_TYPES = ("uint80", "int256", "uint256", "uint256", "uint80")
SLOT0_TYPES = ("uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool")


class RPCError(ValueError):
    pass


class ReorgDetected(RPCError):
    pass


def call_data(signature: str, types: tuple[str, ...] = (), args: tuple = ()) -> str:
    return "0x" + (keccak(text=signature)[:4] + encode(types, args)).hex()


def decode_return(types: tuple[str, ...], value: str) -> tuple:
    try:
        if not isinstance(value, str) or not value.startswith("0x") or len(value) > 32768:
            raise ValueError("invalid contract return data")
        raw = bytes.fromhex(value[2:])
        decoded = decode(types, raw, strict=True)
        if encode(types, decoded) != raw:
            raise ValueError("noncanonical contract return data")
        return decoded
    except (DecodingError, EncodingError, ValueError, TypeError) as error:
        raise ValueError("invalid ABI response") from error


def quantity(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(
        r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})", value
    ):
        raise RPCError("invalid RPC quantity")
    try:
        return int(value, 16)
    except ValueError as error:
        raise RPCError("invalid RPC quantity") from error


class EthereumRPC:
    def __init__(self, client: httpx.AsyncClient, url: str):
        self.client = client
        self.url = url
        self.ids = itertools.count(1)

    async def request(self, method: str, params: list):
        request_id = next(self.ids)
        response = await self.client.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise RPCError("invalid RPC envelope")
        if type(payload.get("id")) is not int or payload["id"] != request_id:
            raise RPCError("RPC response identity mismatch")
        if "error" in payload or payload.get("result") is None:
            raise RPCError("RPC returned an error or empty result")
        return payload["result"]

    async def block(self, tag: Literal["latest", "safe", "finalized"]) -> ChainBlock:
        chain_id = quantity(await self.request("eth_chainId", []))
        if chain_id != 1:
            raise RPCError("expected Ethereum mainnet (chain ID 1)")
        payload = await self.request("eth_getBlockByNumber", [tag, False])
        if not isinstance(payload, dict):
            raise RPCError("invalid block result")
        try:
            block = ChainBlock(
                number=quantity(payload["number"]),
                hash=payload["hash"],
                timestamp=epoch_seconds(quantity(payload["timestamp"])),
                tag=tag,
            )
        except (KeyError, TypeError) as error:
            raise RPCError("incomplete block result") from error
        if block.timestamp > datetime.now(UTC):
            raise RPCError("RPC block timestamp is in the future")
        return block

    async def call(self, address: str, data: str, block: ChainBlock) -> str:
        return await self.request(
            "eth_call",
            [
                {"to": address, "data": data},
                {"blockHash": block.hash, "requireCanonical": True},
            ],
        )

    async def calls(self, address: str, signatures: dict[str, str], block: ChainBlock) -> dict:
        results = await asyncio.gather(
            *(self.call(address, call_data(signature), block) for signature in signatures.values())
        )
        return dict(zip(signatures, results, strict=True))

    async def ensure_canonical(self, block: ChainBlock) -> None:
        current = await self.request("eth_getBlockByNumber", [hex(block.number), False])
        if not isinstance(current, dict) or current.get("hash") != block.hash:
            raise ReorgDetected("block changed during source read")
