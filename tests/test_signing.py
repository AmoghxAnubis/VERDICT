import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from verdict.config import get_settings
from verdict.dashboard import seed_demo
from verdict.evidence import build_bundle
from verdict.signing import (
    SECP256K1_N,
    DiagnosticMessage,
    ReportDomain,
    ReportPolicy,
    SignedDiagnosticReport,
    SignerAuthorization,
    SignerRegistry,
    report_digest,
    revoke_registry,
    rotate_registry,
    sign_report,
    signable,
    typed_data,
    unix_seconds,
    validate_successor,
    verify_report,
)
from verdict.signing_keys import create_keystore, unlock_keystore, write_new_json
from verdict.storage import ObservationStore, build_engine

# Deliberately public test-only keys, never used for deployments.
FIRST = Account.from_key(bytes.fromhex("11" * 32))
SECOND = Account.from_key(bytes.fromhex("22" * 32))
ROOT = Path(__file__).resolve().parents[1]
DATASET = "replay:adv-synthetic-baseline"
OBSERVED = unix_seconds(datetime(2026, 10, 20, 0, 1, tzinfo=UTC))


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp("signing")
    url = f"sqlite:///{(directory / 'test.db').as_posix()}"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("VERDICT_DATABASE_URL", url)
        get_settings.cache_clear()
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    get_settings.cache_clear()
    engine = build_engine(url)
    try:
        store = ObservationStore(engine)
        seed_demo(store, ROOT / "benchmarks/adversarial-v1.json", case="baseline")
        first = store.confidence_rounds(dataset=DATASET, limit=100)[-1]
        yield build_bundle(store, dataset=DATASET, assessment_id=first["report_id"])
    finally:
        engine.dispose()


@pytest.fixture
def policy():
    return ReportPolicy(
        version="diagnostic-local-v1",
        dataset=DATASET,
        domain=ReportDomain(chain_id=31337, verifying_contract="0x" + "12" * 20),
    )


@pytest.fixture
def registry(policy):
    return SignerRegistry(
        domain=policy.domain,
        dataset=DATASET,
        policy_id=policy.policy_id,
        revision=1,
        keys=(
            SignerAuthorization(
                key_id="first",
                signer=FIRST.address,
                valid_from=OBSERVED - 60,
                valid_until=OBSERVED + 3600,
            ),
        ),
    )


@pytest.fixture
def signed(bundle, policy, registry):
    return sign_report(
        bundle,
        policy=policy,
        registry=registry,
        key_id="first",
        account=FIRST,
        issued_at=OBSERVED + 1,
    )


def verify(signed, bundle, policy, registry, *, now=OBSERVED + 2, expected_id=None):
    return verify_report(
        signed,
        bundle=bundle,
        policy=policy,
        registry=registry,
        expected_registry_id=expected_id or registry.registry_id,
        now=now,
    )


def test_digest_matches_independent_solidity_abi_and_json_typed_data(
    signed, bundle, policy, registry
):
    # This consumer declaration intentionally does not reuse REPORT_FIELDS.
    declaration = (
        "DiagnosticReport(uint16 schemaVersion,bytes32 feedId,bytes32 datasetId,"
        "bytes32 assessmentId,bytes32 evidenceRoot,bytes32 configurationHash,bytes32 policyId,"
        "bytes32 keyId,uint64 roundId,uint64 observedAt,uint64 issuedAt,uint64 validUntil,"
        "uint256 value,uint16 confidenceBps,uint8 status,uint8 decimals,bool publicationReady)"
    )
    domain = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                keccak(
                    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
                ),
                keccak(text="VERDICT Diagnostic Reports"),
                keccak(text="1"),
                31337,
                policy.domain.verifying_contract,
            ],
        )
    )
    message = signed.message
    payload = keccak(
        encode(
            ["bytes32"] * 9 + ["uint64"] * 4 + ["uint256", "uint16", "uint8", "uint8", "bool"],
            [
                keccak(text=declaration),
                (1).to_bytes(32, "big"),
                keccak(text="ETH-USD"),
                keccak(text=DATASET),
                bytes.fromhex(message.assessment_id),
                bytes.fromhex(bundle.root),
                bytes.fromhex(message.configuration_hash),
                bytes.fromhex(policy.policy_id),
                keccak(text="first"),
                message.round_id,
                message.observed_at,
                message.issued_at,
                message.valid_until,
                0,
                0,
                0,
                18,
                False,
            ],
        )
    )
    assert keccak(b"\x19\x01" + domain + payload).hex() == signed.digest
    transported = json.loads(json.dumps(typed_data(signed.domain, signed.message)))
    assert (
        Account.recover_message(
            encode_typed_data(full_message=transported), signature=signed.signature
        )
        == FIRST.address
    )
    assert verify(signed, bundle, policy, registry) == signed
    assert signed.message.value is signed.message.confidence is None
    assert signed.message.publication_ready is False
    assert signed == sign_report(
        bundle,
        policy=policy,
        registry=registry,
        key_id="first",
        account=FIRST,
        issued_at=OBSERVED + 1,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("assessment_id", "a" * 64),
        ("evidence_root", "b" * 64),
        ("configuration_hash", "c" * 64),
        ("policy_id", "d" * 64),
        ("dataset", "live"),
        ("key_id", "other"),
        ("issued_at", OBSERVED + 3),
        ("valid_until", OBSERVED + 29),
        ("round_id", OBSERVED + 1),
        ("status", "healthy"),
        ("value", "100"),
        ("confidence", 9700),
        ("publication_ready", True),
    ],
)
def test_mutations_cannot_be_relabelled_as_authorized_reports(
    signed, bundle, policy, registry, field, value
):
    changed = signed.model_copy(
        update={"message": signed.message.model_copy(update={field: value})}
    )
    with pytest.raises(ValueError):
        verify(changed, bundle, policy, registry)


@pytest.mark.parametrize(
    "field,value", [("chain_id", 31338), ("verifying_contract", "0x" + "34" * 20)]
)
def test_domain_change_has_a_different_digest_and_signer(
    signed, bundle, policy, registry, field, value
):
    domain = signed.domain.model_copy(update={field: value})
    assert report_digest(domain, signed.message) != signed.digest
    assert (
        Account.recover_message(signable(domain, signed.message), signature=signed.signature)
        != FIRST.address
    )
    with pytest.raises(ValueError, match="domain"):
        verify(signed.model_copy(update={"domain": domain}), bundle, policy, registry)


@pytest.mark.parametrize("offset,valid", [(0, False), (1, True), (29, True), (30, False)])
def test_report_expiry_is_half_open(signed, bundle, policy, registry, offset, valid):
    if valid:
        assert verify(signed, bundle, policy, registry, now=OBSERVED + offset)
    else:
        with pytest.raises(ValueError, match="not yet valid|expired"):
            verify(signed, bundle, policy, registry, now=OBSERVED + offset)


@pytest.mark.parametrize("issued", [OBSERVED - 1, OBSERVED + 30, True])
def test_stale_future_or_invalid_issuance_is_rejected(bundle, policy, registry, issued):
    with pytest.raises(ValueError):
        sign_report(
            bundle,
            policy=policy,
            registry=registry,
            key_id="first",
            account=FIRST,
            issued_at=issued,
        )


def test_wrong_key_registry_digest_or_signature_is_rejected(signed, bundle, policy, registry):
    with pytest.raises(ValueError, match="trusted registry"):
        verify(signed, bundle, policy, registry, expected_id="0" * 64)
    with pytest.raises(ValueError, match="unlocked key"):
        sign_report(
            bundle,
            policy=policy,
            registry=registry,
            key_id="first",
            account=SECOND,
            issued_at=OBSERVED,
        )
    for changes in ({"digest": "0" * 64}, {"signer": SECOND.address}):
        with pytest.raises(ValueError):
            verify(signed.model_copy(update=changes), bundle, policy, registry)
    other = SECOND.sign_message(signable(signed.domain, signed.message))
    with pytest.raises(ValueError, match="authorized signer"):
        verify(
            signed.model_copy(update={"signature": "0x" + bytes(other.signature).hex()}),
            bundle,
            policy,
            registry,
        )
    raw = bytes.fromhex(signed.signature[2:])
    high_s = SECP256K1_N - int.from_bytes(raw[32:64], "big")
    malleable = raw[:32] + high_s.to_bytes(32, "big") + bytes([55 - raw[64]])
    for bad in (malleable, raw[:64] + b"\x00", b"\x00" * 64 + b"\x1b"):
        with pytest.raises(ValueError, match="canonical low-s"):
            verify(
                signed.model_copy(update={"signature": "0x" + bad.hex()}), bundle, policy, registry
            )


def test_rotation_preserves_existing_reports_and_controls_both_activation_windows(
    bundle, policy, registry
):
    old = registry.model_copy(
        update={"keys": (registry.keys[0].model_copy(update={"valid_until": OBSERVED + 20}),)}
    )
    original = sign_report(
        bundle, policy=policy, registry=old, key_id="first", account=FIRST, issued_at=OBSERVED + 1
    )
    rotated = rotate_registry(
        old,
        old_key_id="first",
        new_key_id="second",
        new_signer=SECOND.address,
        activate_at=OBSERVED + 5,
        retire_at=OBSERVED + 20,
        valid_until=OBSERVED + 3600,
    )
    assert rotated.previous_registry_id == old.registry_id and rotated.revision == 2
    assert verify(original, bundle, policy, rotated, now=OBSERVED + 19)
    with pytest.raises(ValueError, match="expired"):
        verify(original, bundle, policy, rotated, now=OBSERVED + 20)
    with pytest.raises(ValueError, match="inactive"):
        sign_report(
            bundle,
            policy=policy,
            registry=rotated,
            key_id="second",
            account=SECOND,
            issued_at=OBSERVED + 4,
        )
    successor = sign_report(
        bundle,
        policy=policy,
        registry=rotated,
        key_id="second",
        account=SECOND,
        issued_at=OBSERVED + 5,
    )
    assert verify(successor, bundle, policy, rotated, now=OBSERVED + 6)
    with pytest.raises(ValueError, match="inactive"):
        sign_report(
            bundle,
            policy=policy,
            registry=rotated,
            key_id="first",
            account=FIRST,
            issued_at=OBSERVED + 20,
        )
    with pytest.raises(ValueError, match="trusted registry"):
        verify(original, bundle, policy, old, expected_id=rotated.registry_id)


def test_revocation_applies_to_reports_issued_before_the_compromise(
    signed, bundle, policy, registry
):
    revoked = revoke_registry(registry, key_id="first", at=OBSERVED + 3)
    assert verify(signed, bundle, policy, revoked, now=OBSERVED + 2)
    with pytest.raises(ValueError, match="revoked"):
        verify(signed, bundle, policy, revoked, now=OBSERVED + 3)
    with pytest.raises(ValueError, match="inactive|revoked"):
        sign_report(
            bundle,
            policy=policy,
            registry=revoked,
            key_id="first",
            account=FIRST,
            issued_at=OBSERVED + 3,
        )
    with pytest.raises(ValueError, match="extended"):
        revoke_registry(revoked, key_id="first", at=OBSERVED + 4)
    extended = revoked.model_copy(
        update={"revision": 3, "previous_registry_id": revoked.registry_id, "keys": registry.keys}
    )
    with pytest.raises(ValueError, match="extended"):
        validate_successor(revoked, extended)


def test_registry_successors_cannot_extend_remove_or_reassign_existing_keys(registry):
    for changes in (
        {"valid_until": registry.keys[0].valid_until + 1},
        {"signer": SECOND.address},
        {"valid_from": OBSERVED - 120},
        {"key_id": "replacement"},
    ):
        successor = registry.model_copy(
            update={
                "revision": 2,
                "previous_registry_id": registry.registry_id,
                "keys": (registry.keys[0].model_copy(update=changes),),
            }
        )
        with pytest.raises(ValueError):
            validate_successor(registry, successor)
    with pytest.raises(ValueError, match="predecessor"):
        validate_successor(registry, registry)
    with pytest.raises(ValueError, match="unique"):
        rotate_registry(
            registry,
            old_key_id="first",
            new_key_id="first",
            new_signer=SECOND.address,
            activate_at=OBSERVED + 5,
            retire_at=OBSERVED + 20,
            valid_until=OBSERVED + 3600,
        )


@pytest.fixture(scope="module")
def encrypted(tmp_path_factory):
    password = "test-only signing passphrase"
    data, signer = create_keystore(password)
    path = tmp_path_factory.mktemp("signing-key") / "test.keystore.json"
    write_new_json(path, data)
    return data, signer, path, password


def test_encrypted_keystore_round_trip_sanitized_errors_and_exclusive_files(
    encrypted, tmp_path, monkeypatch
):
    data, signer, path, password = encrypted
    account = unlock_keystore(path, password)
    assert account.address == signer
    serialized = path.read_text()
    assert account.key.hex() not in serialized and password not in serialized
    assert data["crypto"]["kdfparams"]["n"] == 262144
    with pytest.raises(ValueError, match="^unable to unlock the signing keystore$"):
        unlock_keystore(path, password + "incorrect")
    with pytest.raises(FileExistsError):
        write_new_json(path, {"overwrite": True})
    bad = json.loads(serialized)
    bad["crypto"]["kdfparams"]["n"] = 2**40
    bad_path = tmp_path / "bad.keystore.json"
    write_new_json(bad_path, bad)
    called = []
    monkeypatch.setattr("verdict.signing_keys.Account.decrypt", lambda *args: called.append(True))
    with pytest.raises(ValueError, match="unable to unlock"):
        unlock_keystore(bad_path, password)
    assert not called


def test_offline_cli_sign_verify_rotation_revocation_and_no_secret_output(
    bundle, encrypted, tmp_path, monkeypatch, capsys
):
    from verdict.cli import main

    _, signer, keystore, password = encrypted
    monkeypatch.setattr(
        "verdict.cli.build_engine", lambda *args: pytest.fail("signing accessed the database")
    )
    monkeypatch.setenv("VERDICT_TEST_SIGNING_PASSWORD", password)
    files = {
        name: tmp_path / f"{name}.json"
        for name in ("policy", "registry", "report", "typed", "bundle", "rotated", "revoked")
    }
    write_new_json(files["bundle"], bundle.model_dump(mode="json"))

    def stamp(offset):
        return (datetime.fromtimestamp(OBSERVED, UTC) + timedelta(seconds=offset)).isoformat()

    def cli(*args):
        monkeypatch.setattr("sys.argv", ["verdict", "signing", *map(str, args)])
        main()
        captured = capsys.readouterr()
        assert password not in captured.out + captured.err
        return json.loads(captured.out)

    cli(
        "policy",
        "--version",
        "local-v1",
        "--dataset",
        DATASET,
        "--chain-id",
        31337,
        "--verifying-contract",
        "0x" + "12" * 20,
        "--output",
        files["policy"],
    )
    registry_id = cli(
        "registry-init",
        "--policy",
        files["policy"],
        "--key-id",
        "first",
        "--signer",
        signer,
        "--valid-from",
        stamp(-60),
        "--valid-until",
        stamp(3600),
        "--output",
        files["registry"],
    )["registry_id"]
    common = (
        "--bundle",
        files["bundle"],
        "--policy",
        files["policy"],
        "--registry",
        files["registry"],
    )
    cli(
        "sign",
        *common,
        "--key-id",
        "first",
        "--keystore",
        keystore,
        "--password-env",
        "VERDICT_TEST_SIGNING_PASSWORD",
        "--issued-at",
        stamp(1),
        "--output",
        files["report"],
    )
    result = cli(
        "verify",
        *common,
        "--report",
        files["report"],
        "--registry-id",
        registry_id,
        "--at",
        stamp(2),
    )
    assert result["verified"] and result["value"] is result["confidence"] is None
    cli("typed-data", "--report", files["report"], "--output", files["typed"])
    projection = json.loads(files["typed"].read_text())
    report = SignedDiagnosticReport.model_validate_json(files["report"].read_text())
    assert (
        Account.recover_message(
            encode_typed_data(full_message=projection), signature=report.signature
        )
        == signer
    )
    cli(
        "registry-rotate",
        "--registry",
        files["registry"],
        "--old-key-id",
        "first",
        "--new-key-id",
        "second",
        "--new-signer",
        SECOND.address,
        "--activate-at",
        stamp(5),
        "--retire-at",
        stamp(60),
        "--valid-until",
        stamp(7200),
        "--output",
        files["rotated"],
    )
    revoked = cli(
        "registry-revoke",
        "--registry",
        files["rotated"],
        "--key-id",
        "first",
        "--at",
        stamp(3),
        "--output",
        files["revoked"],
    )
    with pytest.raises(SystemExit):
        cli(
            "verify",
            "--bundle",
            files["bundle"],
            "--policy",
            files["policy"],
            "--registry",
            files["revoked"],
            "--report",
            files["report"],
            "--registry-id",
            revoked["registry_id"],
            "--at",
            stamp(3),
        )
    captured = capsys.readouterr()
    assert password not in captured.out + captured.err
    with pytest.raises(SystemExit):
        cli("typed-data", "--report", files["report"], "--output", files["typed"])


def test_strict_time_domain_and_unavailable_fields():
    for value in (datetime(2026, 1, 1), datetime(2026, 1, 1, microsecond=1, tzinfo=UTC)):
        with pytest.raises(ValueError):
            unix_seconds(value)
    with pytest.raises(ValueError):
        ReportDomain(chain_id=True, verifying_contract="0x" + "12" * 20)
    with pytest.raises(ValueError, match="zero"):
        ReportDomain(chain_id=31337, verifying_contract="0x" + "00" * 20)
    with pytest.raises(ValueError):
        DiagnosticMessage.model_validate({"status": "available"})
