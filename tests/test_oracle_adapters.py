import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from eth_abi import encode
from fastapi.testclient import TestClient

from verdict.api import create_app
from verdict.config import Settings
from verdict.connectors.chainlink import FEED_CALLS
from verdict.connectors.ethereum import (
    ROUND_TYPES,
    SLOT0_TYPES,
    UNISWAP_FACTORY,
    USDC,
    WETH,
    EthereumRPC,
    ReorgDetected,
    RPCError,
    call_data,
    decode_return,
    quantity,
)
from verdict.connectors.pyth import ETH_USD_ID
from verdict.connectors.registry import NORMALIZERS, build_connector, poll_interval
from verdict.connectors.uniswap import POOL_CALLS
from verdict.ingestion import ingest_all, ingest_source
from verdict.models import SOURCES, ChainBlock
from verdict.replay import load_observations, replay
from verdict.statistics import analyze, build_report
from verdict.storage import ObservationConflict, ObservationStore, build_engine

FIXTURE = Path(__file__).resolve().parents[1] / "simulations/six-provider.json"
BASE = datetime(2026, 10, 16, tzinfo=UTC)


def abi(types, values):
    return "0x" + encode(types, values).hex()


@pytest.fixture
def evidence():
    scenario = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {event["source"]: event["payload"] for event in scenario["events"]}


def normalize(source, payload):
    return NORMALIZERS[source](payload, received_at=BASE + timedelta(seconds=5), latency_ms=1)


def rpc_handler(evidence, *, reorg=False, chain_id="0x1"):
    pool = evidence["uniswap_v3"]
    block = ChainBlock.model_validate(pool["block"])
    calls = {}
    for feed in (evidence["chainlink"], pool["conversion"]):
        for name, signature in FEED_CALLS.items():
            calls[feed["contract"], call_data(signature)] = feed["calls"][name]
    for name, signature in POOL_CALLS.items():
        calls[pool["pool"], call_data(signature)] = pool["calls"][name]
    for token in (USDC, WETH):
        calls[token, call_data("decimals()")] = pool["token_decimals"][token]
    calls[
        UNISWAP_FACTORY,
        call_data(
            "getPool(address,address,uint24)", ("address", "address", "uint24"), (USDC, WETH, 500)
        ),
    ] = pool["factory_pool_result"]

    def handler(request):
        payload = json.loads(request.content)
        method, params = payload["method"], payload["params"]
        assert "Authorization" not in request.headers
        if method == "eth_chainId":
            result = chain_id
        elif method == "eth_getBlockByNumber":
            result = {
                "number": hex(block.number),
                "hash": block.hash,
                "timestamp": hex(int(block.timestamp.timestamp())),
            }
            if reorg and params[0].startswith("0x"):
                result["hash"] = "0x" + "cd" * 32
        else:
            assert method == "eth_call"
            assert params[1] == {"blockHash": block.hash, "requireCanonical": True}
            result = calls[params[0]["to"].lower(), params[0]["data"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    return handler


@pytest.mark.parametrize("source", ["chainlink", "uniswap_v3"])
@pytest.mark.parametrize("tag", ["latest", "safe", "finalized"])
def test_rpc_connectors_pin_block_hash_and_preserve_requested_tag(evidence, source, tag):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(rpc_handler(evidence))
        ) as client:
            settings = Settings(
                ethereum_rpc_url="https://rpc.test", ethereum_block_tag=tag, _env_file=None
            )
            result = await build_connector(source, client, settings).fetch()
            assert result.value == 100
            assert result.provenance.block.tag == tag
            assert result.provenance.verification == "rpc_checked"
            assert result.raw_payload["block"]["hash"] == evidence[source]["block"]["hash"]

    asyncio.run(run())


@pytest.mark.parametrize("source", ["chainlink", "uniswap_v3"])
@pytest.mark.parametrize("failure", ["reorg", "chain"])
def test_rpc_connector_rejects_reorgs_and_wrong_network(evidence, source, failure):
    async def run():
        handler = rpc_handler(
            evidence, reorg=failure == "reorg", chain_id="0x2" if failure == "chain" else "0x1"
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ReorgDetected if failure == "reorg" else RPCError):
                await build_connector(source, client, Settings(_env_file=None)).fetch()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"jsonrpc": "1.0", "id": 1, "result": "0x1"},
        {"jsonrpc": "2.0", "id": True, "result": "0x1"},
        {"jsonrpc": "2.0", "id": 2, "result": "0x1"},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}},
        {"jsonrpc": "2.0", "id": 1, "result": None},
    ],
)
def test_rpc_rejects_invalid_envelopes(payload):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client:
            with pytest.raises(RPCError):
                await EthereumRPC(client, "https://rpc.test").request("eth_chainId", [])

    asyncio.run(run())


@pytest.mark.parametrize("value", [None, True, "0x", "0x01", "0x-1", "0x 1", "1"])
def test_rpc_quantities_are_canonical(value):
    with pytest.raises(RPCError):
        quantity(value)


def test_abi_rejects_trailing_bytes_and_invalid_encoding():
    assert call_data("latestRoundData()") == "0xfeaf968c"
    assert call_data("decimals()") == "0x313ce567"
    assert decode_return(("uint8",), abi(("uint8",), (8,))) == (8,)
    for value in ("0x", "0xzz", abi(("uint8",), (8,)) + "00" * 32):
        with pytest.raises(ValueError, match="ABI"):
            decode_return(("uint8",), value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", abi(("string",), ("BTC / USD",))),
        ("decimals", abi(("uint8",), (18,))),
        ("round", abi(ROUND_TYPES, (1, -1, 1792108800, 1792108800, 1))),
        ("round", abi(ROUND_TYPES, (1, 100, 0, 1792108800, 1))),
        ("round", abi(ROUND_TYPES, (1, 100, 1792108800, 1792108805, 1))),
    ],
)
def test_chainlink_rejects_wrong_instrument_and_invalid_rounds(evidence, field, value):
    evidence["chainlink"]["calls"][field] = value
    with pytest.raises(ValueError):
        normalize("chainlink", evidence["chainlink"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", "0"),
        ("price", "9223372036854775808"),
        ("price", 100.1),
        ("conf", "18446744073709551616"),
        ("conf", "-1"),
        ("expo", -19),
        ("publish_time", True),
    ],
)
def test_pyth_rejects_invalid_wire_numbers(evidence, field, value):
    evidence["pyth"]["parsed"][0]["price"][field] = value
    with pytest.raises(ValueError):
        normalize("pyth", evidence["pyth"])


@pytest.mark.parametrize("message", ["", "0", "zz", "aa  bb", "aa\nbb"])
def test_pyth_requires_hex_update_evidence(evidence, message):
    evidence["pyth"]["binary"]["data"] = [message]
    with pytest.raises(ValueError):
        normalize("pyth", evidence["pyth"])


def test_pyth_rejects_wrong_feed_and_preserves_confidence(evidence):
    row = normalize("pyth", evidence["pyth"])
    assert row.value == 100
    assert row.provenance.pyth_confidence_usd == Decimal("0.01")
    assert row.provenance.verification == "hermes_unverified"
    assert row.provenance.block is None
    evidence["pyth"]["parsed"][0]["id"] = "00" * 32
    with pytest.raises(ValueError):
        normalize("pyth", evidence["pyth"])


def test_pyth_uses_configured_endpoint_and_keeps_key_out_of_evidence(evidence):
    def handler(request):
        assert str(request.url).startswith("https://hermes.test/api/v2/updates/price/latest?")
        assert request.url.params["ids[]"] == ETH_USD_ID
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(200, json=evidence["pyth"])

    async def run():
        settings = Settings(
            pyth_hermes_url="https://hermes.test/api/", pyth_api_key="test-secret", _env_file=None
        )
        assert "test-secret" not in repr(settings)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            row = await build_connector("pyth", client, settings).fetch()
            assert "test-secret" not in row.model_dump_json()

    asyncio.run(run())


def test_pyth_authentication_failure_is_recorded_without_leaking_credentials(store, monkeypatch):
    original_client = httpx.AsyncClient

    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(401, json={"error": "unauthorized"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            ingest_source(
                "pyth", store, Settings(_env_file=None, pyth_api_key="test-secret"), once=True
            )
        )
    assert store.list() == []
    health = store.health()["pyth"]
    assert health["last_http_status"] == 401
    assert health["last_error_type"] == "HTTPStatusError"
    assert "test-secret" not in str(health)


@pytest.mark.parametrize(
    "failure", ["pair", "decimals", "empty", "locked", "conversion", "block", "pool"]
)
def test_uniswap_rejects_incomparable_or_invalid_evidence(evidence, failure):
    pool = evidence["uniswap_v3"]
    if failure == "pair":
        pool["calls"]["token0"] = abi(("address",), (WETH,))
    elif failure == "decimals":
        pool["token_decimals"][USDC] = abi(("uint8",), (18,))
    elif failure == "empty":
        pool["calls"]["liquidity"] = abi(("uint128",), (0,))
    elif failure == "locked":
        pool["calls"]["slot0"] = abi(SLOT0_TYPES, (2**96, 0, 0, 1, 1, 0, False))
    elif failure == "conversion":
        pool["conversion_max_age_seconds"] = 1
    elif failure == "block":
        pool["conversion"]["block"]["hash"] = "0x" + "cd" * 32
    else:
        pool["pool"] = "0x" + "00" * 20
    with pytest.raises(ValueError):
        normalize("uniswap_v3", pool)


def test_uniswap_applies_actual_usdc_usd_rate(evidence):
    conversion = evidence["uniswap_v3"]["conversion"]
    conversion["calls"]["round"] = abi(ROUND_TYPES, (1, 99000000, 1792108800, 1792108800, 1))
    row = normalize("uniswap_v3", evidence["uniswap_v3"])
    assert row.value == Decimal("99")
    assert row.provenance.quote_conversion.value == Decimal("0.99")
    assert row.provenance.native_wrapping_ratio == "1:1 WETH redemption"
    assert "chainlink_usdc_usd" in row.provenance.dependencies


def test_six_provider_replay_storage_analysis_and_api(store):
    first = replay(FIXTURE, store)
    second = replay(FIXTURE, store)
    assert first["normalized_sha256"] == second["normalized_sha256"]
    assert (first["inserted"], second["inserted"], second["duplicates"]) == (6, 0, 6)
    assert store.list() == []
    reopened = build_engine(str(store.engine.url))
    try:
        rows = ObservationStore(reopened).list(dataset=first["dataset"])
        assert {row.source for row in rows} == set(SOURCES)
        assert {row.source: row for row in rows} == {
            row.source: row for row in load_observations(FIXTURE)
        }
    finally:
        reopened.dispose()
    report = build_report(store, dataset=first["dataset"], cutoff=BASE + timedelta(seconds=10))
    assert report.fresh_sources == report.inlier_sources == 6
    assert report.status == "analyzed"
    assert not report.publication_ready
    assert report.sources[5].provenance.quote_conversion.value == 1
    store.save_round(report)
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        params = {"dataset": first["dataset"]}
        sources = client.get("/v1/sources", params=params).json()
        assert {row["source"] for row in sources} == set(SOURCES)
        assert all(row["connector_health"] is None for row in sources)
        row = client.get("/v1/observations", params={**params, "source": "uniswap_v3"}).json()[0]
        assert row["provenance"]["block"]["chain_id"] == 1
        assert row["provenance"]["quote_conversion"]["value"] == "1.00000000"
        assert client.get(
            f"/v1/rounds/{report.round_id}", params=params
        ).json() == report.model_dump(mode="json")
        assert client.get("/v1/feed/ETH-USD").status_code == 503


def test_same_timestamp_uses_provider_sequence_in_storage_and_analysis(store, evidence):
    payload = evidence["pyth"]
    first = normalize("pyth", payload)
    rows = []
    for slot in range(first.event_sequence, first.event_sequence + 20):
        candidate = deepcopy(payload)
        candidate["parsed"][0]["metadata"]["slot"] = slot
        candidate["parsed"][0]["price"]["price"] = str(10000000000 + slot)
        rows.append(normalize("pyth", candidate))
    # Ensure this case distinguishes sequence ordering from hash ordering.
    assert max(rows, key=lambda row: row.observation_id) != rows[-1]
    store.add_many(reversed(rows))
    assert store.list(source="pyth", limit=1) == [rows[-1]]
    report = build_report(store, dataset="live", cutoff=BASE + timedelta(seconds=10))
    direct = analyze(list(reversed(rows)), dataset="live", cutoff=BASE + timedelta(seconds=10))
    assert report.round_id == direct.round_id
    assert report.sources[4].observation_id == rows[-1].observation_id
    # Even when history has expired, snapshot's latest-per-source row is the same.
    snapshot = store.snapshot(
        dataset="live", cutoff=BASE + timedelta(days=1), history_seconds=10, freshness_seconds=30
    )
    assert snapshot == [rows[-1]]
    with pytest.raises(ObservationConflict):
        store.add(rows[-1].model_copy(update={"event_sequence": 1}))


def test_invalid_oracle_evidence_rolls_back_entire_replay(store, tmp_path):
    scenario = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scenario["events"][-1]["payload"]["conversion_max_age_seconds"] = 1
    path = tmp_path / "invalid-oracle.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    with pytest.raises(ValueError, match="conversion feed is stale"):
        replay(path, store)
    assert store.list(dataset="replay:six-provider") == []
    assert store.health() == {}


def test_onchain_workers_use_slower_interval_for_poll_and_retry(store, evidence, monkeypatch):
    from unittest.mock import AsyncMock

    from verdict.connectors.chainlink import ChainlinkConnector

    row = normalize("chainlink", evidence["chainlink"])
    monkeypatch.setattr(
        ChainlinkConnector, "fetch", AsyncMock(side_effect=[RPCError("offline"), row])
    )
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    settings = Settings(_env_file=None, onchain_poll_interval_seconds=20)
    assert poll_interval("coinbase", settings) == 2
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ingest_source("chainlink", store, settings, sleep=sleep))
    assert delays == [20, 20]
    assert store.health()["chainlink"]["consecutive_failures"] == 0


def test_all_six_workers_persist_independent_health_and_idempotent_receipts(store, monkeypatch):
    from unittest.mock import AsyncMock

    from verdict.connectors.registry import CONNECTORS

    for row in load_observations(FIXTURE):
        live = row.model_copy(update={"dataset": "live"})
        monkeypatch.setattr(CONNECTORS[row.source], "fetch", AsyncMock(return_value=live))
    for _ in range(2):
        results = asyncio.run(ingest_all(store, Settings(_env_file=None), once=True))
        assert [result["source"] for result in results] == list(SOURCES)
        assert all(result["status"] == "ok" for result in results)
    assert len(store.list()) == 6
    assert set(store.health()) == set(SOURCES)
    assert all(
        health["received"] == 2 and health["inserted"] == 1 for health in store.health().values()
    )
