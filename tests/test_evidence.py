import hashlib
import json
from decimal import localcontext
from pathlib import Path

import pytest
from sqlalchemy import delete, update

from verdict.dashboard import seed_demo
from verdict.evidence import (
    DOMAIN,
    EvidenceBundle,
    EvidenceLeaf,
    build_bundle,
    canonical_bytes,
    evidence_root,
    inclusion_proof,
    leaf_hash,
    verify_bundle,
    verify_inclusion,
)
from verdict.storage import ObservationRow, ObservationStore, build_engine

SUITE = Path(__file__).resolve().parents[1] / "benchmarks/adversarial-v1.json"
DATASET = "replay:adv-synthetic-single_spike"


@pytest.fixture
def retained(store):
    seed_demo(store, SUITE, case="single_spike")
    assessment = store.confidence_rounds(dataset=DATASET, limit=1)[0]
    return store, build_bundle(store, dataset=DATASET, assessment_id=assessment["report_id"])


def reseal(bundle, leaves):
    leaves = tuple(sorted(leaves, key=lambda item: (item.kind, item.record_id)))
    return bundle.model_copy(update={"leaves": leaves, "root": evidence_root(leaves)})


def test_canonical_encoding_has_exact_types_lengths_unicode_and_float_bits():
    assert canonical_bytes({"b": [True, None, "é"], "a": 1}).hex() == (
        "0700000002050000000161030000000131050000000162060000000302000500000002c3a9"
    )
    assert canonical_bytes(1) != canonical_bytes(1.0) != canonical_bytes("1")
    assert canonical_bytes(0.0).hex() == "040000000000000000"
    assert canonical_bytes(-0.0).hex() == "048000000000000000"
    assert canonical_bytes("é") != canonical_bytes("e\u0301")
    assert canonical_bytes({"é": 2, "a": 1}) == canonical_bytes({"a": 1, "é": 2})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "key"}, (1, 2), {"x"}])
def test_canonical_encoding_rejects_ambiguous_or_non_json_values(value):
    with pytest.raises(ValueError):
        canonical_bytes(value)


@pytest.mark.parametrize("count", [1, 2, 3, 5, 8, 9, 17, 32])
def test_inclusion_proofs_for_every_position_and_odd_tree(count):
    leaves = tuple(
        EvidenceLeaf(
            dataset="replay:test",
            assessment_id="a" * 64,
            kind="observation",
            record_id=f"{index:064x}",
            payload={"exact_price": "100.000000000000000001", "index": index},
        )
        for index in range(count)
    )
    bundle = EvidenceBundle(
        dataset="replay:test", assessment_id="a" * 64, root=evidence_root(leaves), leaves=leaves
    )
    # Independently spell out the three-leaf shape, including the sealed count.
    if count == 3:

        def h(tag, content):
            return hashlib.sha256(DOMAIN + tag + content).digest()

        hashes = [leaf_hash(leaf) for leaf in leaves]
        expected = h(
            b"\x02", b"\x00\x00\x00\x03" + h(b"\x01", h(b"\x01", hashes[0] + hashes[1]) + hashes[2])
        )
        assert bundle.root == expected.hex()
    for index in range(count):
        proof = inclusion_proof(bundle, index)
        assert verify_inclusion(proof, expected_root=bundle.root)
        assert not verify_inclusion(proof, expected_root="0" * 64)
        assert not verify_inclusion(
            proof.model_copy(update={"leaf_count": count + 1}), expected_root=bundle.root
        )
        assert not verify_inclusion(
            proof.model_copy(update={"siblings": (*proof.siblings, "0" * 64)}),
            expected_root=bundle.root,
        )
        changed = proof.leaf.model_copy(update={"dataset": "live"})
        assert not verify_inclusion(
            proof.model_copy(update={"leaf": changed}), expected_root=bundle.root
        )
    if count > 1:
        proof = inclusion_proof(bundle, 0)
        assert not verify_inclusion(
            proof.model_copy(update={"leaf_index": 1}), expected_root=bundle.root
        )
        assert not verify_inclusion(
            proof.model_copy(update={"siblings": proof.siblings[:-1]}), expected_root=bundle.root
        )


def test_round_bundle_reproduces_attack_history_and_survives_reopening(retained):
    store, bundle = retained
    assert verify_bundle(EvidenceBundle.model_validate_json(bundle.model_dump_json())) == bundle
    weights = [leaf for leaf in bundle.leaves if leaf.kind == "weighting"]
    assert len(weights) == 25
    assert any(
        sample["source"] == "coinbase" and sample["state"] == "quarantined"
        for leaf in weights
        for sample in leaf.payload["sources"]
    )
    with localcontext() as context:
        context.prec = 6
        assert build_bundle(store, dataset=DATASET, assessment_id=bundle.assessment_id) == bundle
    engine = build_engine(str(store.engine.url))
    try:
        assert (
            build_bundle(
                ObservationStore(engine), dataset=DATASET, assessment_id=bundle.assessment_id
            )
            == bundle
        )
    finally:
        engine.dispose()
    with pytest.raises(LookupError):
        build_bundle(store, dataset="live", assessment_id=bundle.assessment_id)


def test_rehashed_tampering_missing_history_reordering_and_extras_fail(retained):
    _, bundle = retained
    with pytest.raises(ValueError, match="ordered"):
        verify_bundle(bundle.model_copy(update={"leaves": tuple(reversed(bundle.leaves))}))
    with pytest.raises(ValueError, match="context"):
        verify_bundle(bundle.model_copy(update={"dataset": "live"}))
    observation = next(leaf for leaf in bundle.leaves if leaf.kind == "observation")
    changed = observation.model_copy(
        update={"payload": {**observation.payload, "raw_payload": {"forged": True}}}
    )
    leaves = tuple(changed if leaf == observation else leaf for leaf in bundle.leaves)
    with pytest.raises(ValueError, match="root"):
        verify_bundle(bundle.model_copy(update={"leaves": leaves}))
    with pytest.raises(ValueError, match="reproduce"):
        verify_bundle(reseal(bundle, leaves))
    noncanonical = observation.model_copy(
        update={"payload": {**observation.payload, "event_sequence": 0}}
    )
    with pytest.raises(ValueError, match="noncanonical"):
        verify_bundle(
            reseal(
                bundle, [noncanonical if leaf == observation else leaf for leaf in bundle.leaves]
            )
        )
    with pytest.raises(ValueError, match="missing observation"):
        verify_bundle(reseal(bundle, [leaf for leaf in bundle.leaves if leaf != observation]))
    genesis = next(
        leaf
        for leaf in bundle.leaves
        if leaf.kind == "weighting" and leaf.payload["previous_report_id"] is None
    )
    with pytest.raises(ValueError, match="missing weighting"):
        verify_bundle(reseal(bundle, [leaf for leaf in bundle.leaves if leaf != genesis]))
    from verdict.models import Observation

    extra = Observation.model_validate({**observation.payload, "provider_event_id": "unrelated"})
    extra_leaf = observation.model_copy(
        update={"record_id": extra.observation_id, "payload": extra.model_dump(mode="json")}
    )
    with pytest.raises(ValueError, match="unrelated"):
        verify_bundle(reseal(bundle, (*bundle.leaves, extra_leaf)))


@pytest.mark.parametrize("mutation", ["missing", "changed"])
def test_export_refuses_missing_or_corrupt_retained_observations(retained, mutation):
    store, bundle = retained
    leaf = next(leaf for leaf in bundle.leaves if leaf.kind == "observation")
    with store.engine.begin() as connection:
        if mutation == "missing":
            connection.execute(delete(ObservationRow).where(ObservationRow.id == leaf.record_id))
        else:
            connection.execute(
                update(ObservationRow)
                .where(ObservationRow.id == leaf.record_id)
                .values(payload=json.dumps({**leaf.payload, "raw_payload": {"corrupt": True}}))
            )
    with pytest.raises(ValueError):
        build_bundle(store, dataset=DATASET, assessment_id=bundle.assessment_id)


def test_cli_export_offline_verify_proof_and_no_overwrite(retained, tmp_path, monkeypatch, capsys):
    from verdict.cli import main

    _, bundle = retained
    output, proof = tmp_path / "bundle.json", tmp_path / "proof.json"

    def cli(*args):
        monkeypatch.setattr("sys.argv", ["verdict", "evidence", *map(str, args)])
        main()
        return json.loads(capsys.readouterr().out)

    assert (
        cli("export", bundle.assessment_id, "--dataset", DATASET, "--output", output)["root"]
        == bundle.root
    )
    with pytest.raises(FileExistsError):
        cli("export", bundle.assessment_id, "--dataset", DATASET, "--output", output)
    monkeypatch.setattr(
        "verdict.cli.build_engine",
        lambda *args: pytest.fail("offline verification accessed storage"),
    )
    assert cli("verify", output)["verified"]
    assert cli("proof", output, "--index", 0, "--output", proof)["root"] == bundle.root
    assert cli("verify-proof", proof, "--root", bundle.root)["verified"]
    with pytest.raises(SystemExit):
        cli("verify-proof", proof, "--root", "0" * 64)
