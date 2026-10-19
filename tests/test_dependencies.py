import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update

from verdict.api import create_app
from verdict.cli import main
from verdict.config import Settings
from verdict.dependencies import DependencyRegistry, bundled_registry, inspect_dependencies
from verdict.models import SOURCES
from verdict.replay import load_observations
from verdict.statistics import analyze
from verdict.storage import DependencyRegistryRow, ObservationStore, build_engine

ROOT = Path(__file__).resolve().parents[1]


def changed(registry, **updates):
    return DependencyRegistry.model_validate({**registry.model_dump(mode="json"), **updates})


def test_seed_evidence_matches_reviewed_code_and_adapter_lineage():
    registry = bundled_registry()
    for evidence in registry.evidence:
        if evidence.kind == "implementation":
            content = (ROOT / evidence.reference).read_text(encoding="utf-8")
            assert evidence.content == content
            assert hashlib.sha256(content.encode()).hexdigest() == evidence.content_sha256
    result = inspect_dependencies(registry)
    by_source = {source["source"]: source for source in result["sources"]}
    rows = load_observations(ROOT / "simulations/six-provider.json")
    for row in rows:
        if row.provenance:
            assert set(row.provenance.dependencies) <= set(
                by_source[row.source]["known_dependencies"]
            )
    assert result["unresolved_sources"] == sorted(SOURCES)
    assert result["independent_domain_count"] is None
    assert result["publication_ready"] is False


def test_transitive_overlaps_are_typed_and_do_not_claim_publisher_identity():
    registry = bundled_registry()
    result = inspect_dependencies(registry, ("uniswap_v3", "chainlink"))
    assert result == inspect_dependencies(registry, ("chainlink", "uniswap_v3"))
    overlaps = {item["node_id"]: item for item in result["shared_dependencies"]}
    assert set(overlaps) == {
        "verdict_collector",
        "chainlink_protocol",
        "configured_ethereum_rpc",
        "ethereum_mainnet",
    }
    assert overlaps["chainlink_protocol"]["kind"] == "protocol"
    assert overlaps["configured_ethereum_rpc"]["sources"] == ["chainlink", "uniswap_v3"]
    uniswap = result["sources"][1]
    assert "chainlink_usdc_usd" in uniswap["known_dependencies"]
    assert "unknown_chainlink_usdc" in uniswap["unknown_dependencies"]
    assert "unknown_chainlink_eth" not in uniswap["unknown_dependencies"]
    assert "unknown_deployment" in uniswap["unknown_dependencies"]
    exchange = inspect_dependencies(registry, ("kraken", "coinbase", "binance_us"))
    assert [item["node_id"] for item in exchange["shared_dependencies"]] == ["verdict_collector"]
    assert len(exchange["unresolved_sources"]) == 3
    assert exchange["independent_domain_count"] is None
    single = inspect_dependencies(registry, ("pyth",))
    assert single["shared_dependencies"] == []
    assert single["unresolved_sources"] == ["pyth"]


def test_canonical_order_timezone_and_evidence_identity():
    original = bundled_registry()
    payload = original.model_dump(mode="json")
    for collection in ("sources", "nodes", "edges", "evidence"):
        payload[collection].reverse()
    for item in (*payload["nodes"], *payload["edges"]):
        item["evidence_ids"].reverse()
    payload["reviewed_at"] = "2026-10-19T23:30:00+05:30"
    equivalent = DependencyRegistry.model_validate(payload)
    assert equivalent.registry_id == original.registry_id
    assert inspect_dependencies(equivalent) == inspect_dependencies(original)
    payload["scope"] += " Another review."
    assert DependencyRegistry.model_validate(payload).registry_id != original.registry_id
    payload = original.model_dump(mode="json")
    payload["evidence"][0]["content"] += "\n# retained revision\n"
    payload["evidence"][0]["content_sha256"] = hashlib.sha256(
        payload["evidence"][0]["content"].encode()
    ).hexdigest()
    revised = DependencyRegistry.model_validate(payload)
    assert revised.registry_id != original.registry_id
    assert (
        inspect_dependencies(revised)["inspection_id"]
        != inspect_dependencies(original)["inspection_id"]
    )


@pytest.mark.parametrize(
    "defect",
    [
        "tampered_evidence",
        "duplicate_node",
        "duplicate_evidence",
        "duplicate_edge",
        "unknown_evidence",
        "duplicate_reference",
        "unknown_node",
        "missing_source",
        "same_root",
        "unreachable",
        "cycle",
        "self_cycle",
        "unknown_relationship",
        "unknown_has_child",
        "future_evidence",
        "naive_review",
        "unused_evidence",
        "no_source_dependencies",
    ],
)
def test_invalid_graphs_fail_closed(defect):
    data = bundled_registry().model_dump(mode="json")
    if defect == "tampered_evidence":
        data["evidence"][0]["content"] += " altered"
    elif defect == "duplicate_node":
        data["nodes"].append(data["nodes"][0])
    elif defect == "duplicate_evidence":
        data["evidence"].append(data["evidence"][0])
    elif defect == "duplicate_edge":
        data["edges"].append(data["edges"][0])
    elif defect == "unknown_evidence":
        data["nodes"][0]["evidence_ids"] = ["missing"]
    elif defect == "duplicate_reference":
        data["nodes"][0]["evidence_ids"] *= 2
    elif defect == "unknown_node":
        data["edges"][0]["upstream"] = "missing"
    elif defect == "missing_source":
        data["sources"][0] = data["sources"][1]
    elif defect == "same_root":
        data["sources"][0]["node_id"] = data["sources"][1]["node_id"]
    elif defect == "unreachable":
        data["nodes"].append({**data["nodes"][0], "id": "isolated"})
    elif defect in ("cycle", "self_cycle", "unknown_has_child"):
        down, up = {
            "cycle": ("verdict_collector", "source:coinbase"),
            "self_cycle": ("source:coinbase", "source:coinbase"),
            "unknown_has_child": ("unknown_deployment", "verdict_collector"),
        }[defect]
        data["edges"].append(
            {**data["edges"][0], "downstream": down, "upstream": up, "relationship": "delivery"}
        )
    elif defect == "unknown_relationship":
        next(edge for edge in data["edges"] if edge["relationship"] == "unknown")[
            "relationship"
        ] = "price"
    elif defect == "future_evidence":
        data["evidence"][0]["captured_at"] = "2026-10-20T00:00:00Z"
    elif defect == "naive_review":
        data["reviewed_at"] = "2026-10-19T18:00:00"
    elif defect == "unused_evidence":
        data["evidence"].append({**data["evidence"][0], "id": "unused"})
    else:
        data["edges"] = [edge for edge in data["edges"] if edge["downstream"] != "source:pyth"]
    with pytest.raises(ValidationError):
        DependencyRegistry.model_validate(data)


@pytest.mark.parametrize("sources", [(), ("kraken", "kraken"), ("unregistered",)])
def test_invalid_source_selection(sources):
    with pytest.raises(ValueError, match="unique registered sources"):
        inspect_dependencies(bundled_registry(), sources)


def test_registry_version_is_immutable_reopenable_and_independent_of_latest(store):
    first = bundled_registry()
    assert store.save_dependency_registry(first)
    assert not store.save_dependency_registry(first)
    with pytest.raises(ValueError, match="version conflict"):
        store.save_dependency_registry(changed(first, scope="Changed same version"))
    later = changed(
        first,
        version="dependencies-v2",
        scope="New review, explicitly selected",
        reviewed_at=first.reviewed_at + timedelta(days=1),
    )
    assert store.save_dependency_registry(later)
    reopened = build_engine(str(store.engine.url))
    try:
        other = ObservationStore(reopened)
        assert other.get_dependency_registry(first.registry_id) == first
        assert other.get_dependency_registry(later.registry_id) == later
        assert other.get_dependency_registry("0" * 64) is None
        assert [row["registry_id"] for row in other.dependency_registries()] == [
            later.registry_id,
            first.registry_id,
        ]
        assert len(other.dependency_registries(limit=1)) == 1
        assert inspect_dependencies(other.get_dependency_registry(first.registry_id)) == (
            inspect_dependencies(first)
        )
    finally:
        reopened.dispose()


def test_unvalidated_model_copy_is_revalidated_at_storage_and_inspection_boundaries(store):
    forged = bundled_registry().model_copy(update={"nodes": ()})
    with pytest.raises(ValidationError):
        store.save_dependency_registry(forged)
    with pytest.raises(ValidationError):
        inspect_dependencies(forged)
    assert store.dependency_registries() == []


def test_concurrent_registration_is_idempotent(store):
    registry = bundled_registry()

    async def run():
        return await asyncio.gather(
            *(asyncio.to_thread(store.save_dependency_registry, registry) for _ in range(4))
        )

    assert sorted(asyncio.run(run())) == [False, False, False, True]
    assert len(store.dependency_registries()) == 1


def test_corrupted_stored_registry_is_not_served(store):
    registry = bundled_registry()
    store.save_dependency_registry(registry)
    data = registry.model_dump(mode="json")
    data["scope"] = "Corrupted stored content with valid schema"
    with store.session() as session, session.begin():
        session.execute(update(DependencyRegistryRow).values(payload=json.dumps(data)))
    with pytest.raises(ValueError, match="checksum mismatch"):
        store.get_dependency_registry(registry.registry_id)


def test_upgrade_from_0005_preserves_observations_rounds_and_receipts(store, observation):
    from verdict.queue_models import QueuedAttempt

    config = Config(str(ROOT / "alembic.ini"))
    command.downgrade(config, "0005")
    store.add(observation)
    round_ = analyze([observation], dataset=observation.dataset, cutoff=observation.received_at)
    store.save_round(round_)
    attempt = QueuedAttempt(source="coinbase", error_type="ConnectError")
    store.consume_attempt(attempt)
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        assert client.get("/health/ready").status_code == 503
    command.upgrade(config, "head")
    command.check(config)
    store.check_ready()
    assert store.list(dataset=observation.dataset) == [observation]
    assert store.get_round(round_.round_id, dataset=observation.dataset) == round_.model_dump(
        mode="json"
    )
    assert store.consume_attempt(attempt) == "duplicate_attempt"
    assert store.save_dependency_registry(bundled_registry())


def test_cli_and_read_only_api_are_pinned_to_explicit_registry(store, monkeypatch, capsys):
    registry = bundled_registry()
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        assert client.get("/v1/dependencies").json() == []
        path = f"/v1/dependencies/{registry.registry_id}"
        assert client.get(path).status_code == 404
        monkeypatch.setattr("sys.argv", ["verdict", "dependencies", "register"])
        main()
        saved = json.loads(capsys.readouterr().out)
        assert saved == {
            "registry_id": registry.registry_id,
            "version": registry.version,
            "inserted": True,
        }
        main()
        assert json.loads(capsys.readouterr().out)["inserted"] is False
        monkeypatch.setattr(
            "sys.argv",
            [
                "verdict",
                "dependencies",
                "inspect",
                registry.registry_id,
                "--source",
                "uniswap_v3",
                "--source",
                "chainlink",
            ],
        )
        main()
        result = json.loads(capsys.readouterr().out)
        assert (
            result
            == client.get(
                path + "/inspection", params={"source": ["chainlink", "uniswap_v3"]}
            ).json()
        )
        assert client.get(path).json() == registry.envelope()
        assert client.get(path + "/inspection").json() == inspect_dependencies(registry)
        assert client.get("/v1/dependencies").json()[0]["registry_id"] == registry.registry_id
        assert client.get("/v1/dependencies", params={"limit": 0}).status_code == 422
        assert client.get("/v1/dependencies/invalid").status_code == 422
        assert client.get(path + "/inspection", params={"source": "invented"}).status_code == 422
        assert (
            client.get(path + "/inspection", params={"source": ["pyth", "pyth"]}).status_code == 422
        )
        assert client.post(path, json=registry.envelope()).status_code == 405
        assert client.get("/v1/feed/ETH-USD").status_code == 503
        assert store.rounds() == []
        assert store.list() == []


def test_cli_import_custom_file_and_reject_missing_or_duplicate_selection(
    store, monkeypatch, tmp_path, capsys
):
    registry = changed(bundled_registry(), version="review-custom")
    path = tmp_path / "registry.json"
    path.write_text(registry.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["verdict", "dependencies", "register", "--file", str(path)])
    main()
    assert json.loads(capsys.readouterr().out)["registry_id"] == registry.registry_id
    for args in [
        ["0" * 64],
        [registry.registry_id, "--source", "pyth", "--source", "pyth"],
    ]:
        monkeypatch.setattr("sys.argv", ["verdict", "dependencies", "inspect", *args])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2
    with store.session() as session:
        assert len(list(session.scalars(select(DependencyRegistryRow)))) == 1
