"""Execute compiled Solidity in a real Py-EVM chain with Python evidence/signatures."""

import ast
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from eth_abi import decode, encode
from eth_account import Account
from eth_tester import EthereumTester, PyEVMBackend
from eth_tester.exceptions import TransactionFailed
from eth_utils import keccak, to_checksum_address

from verdict.config import get_settings
from verdict.contract_inputs import contract_inputs, registry_inputs
from verdict.dashboard import seed_demo
from verdict.evidence import build_bundle
from verdict.signing import (
    REPORT_FIELDS,
    SECP256K1_N,
    ReportDomain,
    ReportPolicy,
    SignerAuthorization,
    SignerRegistry,
    report_digest,
    revoke_registry,
    rotate_registry,
    sign_report,
    signable,
    typed_data,
    unix_seconds,
)
from verdict.signing_keys import write_new_json
from verdict.storage import ObservationStore, build_engine

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
BASE = datetime(2030, 1, 1, tzinfo=UTC)  # Frozen future fixture avoids host-clock mining jumps.
OBSERVED = unix_seconds(BASE) + 100
DATASET = "replay:adv-synthetic-baseline"
FIRST = Account.from_key(bytes.fromhex("11" * 32))  # Public local test keys only.
SECOND = Account.from_key(bytes.fromhex("22" * 32))


def artifact(name):
    value = json.loads((CONTRACTS / "artifacts" / f"{name}.json").read_text(encoding="utf-8"))
    for source, expected in value["sourceSha256"].items():
        normalized = (CONTRACTS / source).read_text(encoding="utf-8").replace("\r\n", "\n")
        assert hashlib.sha256(normalized.encode()).hexdigest() == expected, (
            "run contracts/npm run compile"
        )
    lock = (CONTRACTS / "package-lock.json").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert hashlib.sha256(lock.encode()).hexdigest() == value["packageLockSha256"]
    return value


def abi_type(field):
    if field["type"].startswith("tuple"):
        return (
            "(" + ",".join(abi_type(item) for item in field["components"]) + ")" + field["type"][5:]
        )
    return field["type"]


def abi_value(field, value):
    if field["type"] == "tuple":
        return tuple(abi_value(part, value[part["name"]]) for part in field["components"])
    if field["type"] == "tuple[]":
        element = {**field, "type": "tuple"}
        return [abi_value(element, entry) for entry in value]
    if field["type"].startswith("bytes") and isinstance(value, str):
        return bytes.fromhex(value.removeprefix("0x"))
    return value


class Contract:
    def __init__(self, tester, name, arguments=(), *, at=None):
        self.tester, self.artifact = tester, artifact(name)
        self.owner = tester.get_accounts()[0]
        self.functions = {
            item["name"]: item for item in self.artifact["abi"] if item["type"] == "function"
        }
        if at:
            self.address = at
            return
        constructor = next(item for item in self.artifact["abi"] if item["type"] == "constructor")
        fields = constructor["inputs"]
        data = (
            self.artifact["bytecode"]
            + encode(
                [abi_type(f) for f in fields],
                [abi_value(f, v) for f, v in zip(fields, arguments, strict=True)],
            ).hex()
        )
        receipt = tester.get_transaction_receipt(
            tester.send_transaction({"from": self.owner, "gas": 12_000_000, "data": data})
        )
        assert receipt["status"] == 1
        self.address = receipt["contract_address"]

    def transaction(self, name, *arguments, sender=None):
        method = self.functions[name]
        fields = method["inputs"]
        declaration = name + "(" + ",".join(abi_type(f) for f in fields) + ")"
        data = keccak(text=declaration)[:4] + encode(
            [abi_type(f) for f in fields],
            [abi_value(f, v) for f, v in zip(fields, arguments, strict=True)],
        )
        return {
            "from": sender or self.owner,
            "to": self.address,
            "gas": 12_000_000,
            "data": "0x" + data.hex(),
        }

    def call(self, name, *arguments, sender=None):
        raw = self.tester.call(self.transaction(name, *arguments, sender=sender))
        types = [abi_type(field) for field in self.functions[name]["outputs"]]
        return decode(types, bytes.fromhex(raw[2:]))

    def tx(self, name, *arguments, sender=None, succeeds=True, gas=None):
        transaction = self.transaction(name, *arguments, sender=sender)
        if gas is not None:
            transaction["gas"] = gas
        receipt = self.tester.get_transaction_receipt(self.tester.send_transaction(transaction))
        assert receipt["status"] == int(succeeds)
        return receipt

    def rejects(self, name, *arguments, error=None, sender=None):
        with pytest.raises(TransactionFailed) as caught:
            self.call(name, *arguments, sender=sender)
        if error:
            raw = ast.literal_eval(str(caught.value))
            assert raw[:4] == keccak(text=error)[:4]


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    directory = tmp_path_factory.mktemp("contract-evidence")
    url = f"sqlite:///{(directory / 'evidence.db').as_posix()}"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("VERDICT_DATABASE_URL", url)
        patch.setattr("verdict.adversarial.BASE", BASE)
        get_settings.cache_clear()
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        engine = build_engine(url)
        try:
            store = ObservationStore(engine)
            seed_demo(store, ROOT / "benchmarks/adversarial-v1.json", case="baseline")
            history = {
                unix_seconds(datetime.fromisoformat(row["cutoff"].replace("Z", "+00:00"))): row
                for row in store.confidence_rounds(dataset=DATASET, limit=100)
            }
            result = [
                build_bundle(store, dataset=DATASET, assessment_id=history[at]["report_id"])
                for at in (OBSERVED, OBSERVED + 10)
            ]
            assert history[OBSERVED]["candidate_value"] is not None
        finally:
            engine.dispose()
    get_settings.cache_clear()
    return result


def registry_grants(registry):
    return sorted(
        [
            {
                "keyId": "0x" + keccak(text=key.key_id).hex(),
                "signer": key.signer,
                "validFrom": key.valid_from,
                "validUntil": key.valid_until,
                "revokedAt": key.revoked_at if key.revoked_at is not None else 0,
                "revoked": key.revoked_at is not None,
            }
            for key in registry.keys
        ],
        key=lambda key: key["keyId"],
    )


class Scenario:
    def __init__(self, bundles):
        self.bundles = bundles
        self.tester = EthereumTester(
            PyEVMBackend(genesis_parameters={"timestamp": OBSERVED - 20, "gas_limit": 30_000_000})
        )
        self.owner, self.relayer, self.attacker = self.tester.get_accounts()[:3]
        self.oracle = Contract(self.tester, "DiagnosticOracle", (self.owner, keccak(text=DATASET)))
        self.consumer = Contract(self.tester, "FailClosedConsumer", (self.oracle.address, 30, 7500))
        self.policy = ReportPolicy(
            version="local-evm-v1",
            dataset=DATASET,
            domain=ReportDomain(
                chain_id=self.tester.backend.chain.chain_id, verifying_contract=self.oracle.address
            ),
        )
        self.registry = SignerRegistry(
            domain=self.policy.domain,
            dataset=DATASET,
            policy_id=self.policy.policy_id,
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
        self.signed = self.sign()
        self.payload = contract_inputs(
            self.signed,
            bundle=bundles[0],
            policy=self.policy,
            registry=self.registry,
            expected_registry_id=self.registry.registry_id,
            now=OBSERVED + 1,
        )
        self.report = self.payload["report"]
        self.signature = self.payload["signature"]

    def sign(
        self, *, bundle=None, registry=None, key_id="first", account=FIRST, issued_at=OBSERVED + 1
    ):
        return sign_report(
            bundle or self.bundles[0],
            policy=self.policy,
            registry=registry or self.registry,
            key_id=key_id,
            account=account,
            issued_at=issued_at,
        )

    def initialize(self):
        p = self.payload
        return self.oracle.tx(
            "initialize",
            p["policy_id"],
            p["configuration_hash"],
            p["maximum_ttl"],
            p["maximum_age"],
            p["registry_id"],
            p["grants"],
        )

    def warp(self, timestamp):
        if self.tester.get_block_by_number("latest")["timestamp"] < timestamp:
            self.tester.time_travel(timestamp)
            self.tester.mine_blocks()
        assert self.tester.get_block_by_number("latest")["timestamp"] == timestamp

    def apply(self, registry):
        projection = registry_inputs(
            policy=self.policy, registry=registry, expected_registry_id=registry.registry_id
        )
        return self.oracle.tx(
            "applyRegistry",
            projection["previous_registry_id"],
            projection["registry_id"],
            projection["registry_revision"],
            projection["grants"],
        )


@pytest.fixture
def scenario(bundles):
    return Scenario(bundles)


def test_python_signature_and_verified_bundle_reach_the_real_evm(scenario):
    s = scenario
    assert s.oracle.call("hashReport", s.report)[0].hex() == s.signed.digest
    assert s.consumer.call("canAct") == (False,)
    s.consumer.rejects("readPrice", error="FeedUnavailable()")
    s.oracle.rejects("submit", s.report, s.signature, error="NotInitialized()")
    s.initialize()
    s.warp(OBSERVED + 1)
    receipt = s.oracle.tx("submit", s.report, s.signature, sender=s.relayer)
    accepted = next(
        log
        for log in receipt["logs"]
        if log["topics"][0]
        == "0x"
        + keccak(text="DiagnosticAccepted(bytes32,bytes32,bytes32,uint64,address,uint64)").hex()
    )
    assert accepted["topics"][1:] == [
        s.report["assessmentId"],
        s.report["evidenceRoot"],
        s.payload["digest"],
    ]
    event_round, event_signer, event_expiry = decode(
        ["uint64", "address", "uint64"], bytes.fromhex(accepted["data"][2:])
    )
    assert event_round == OBSERVED and to_checksum_address(event_signer) == FIRST.address
    assert event_expiry == s.signed.message.valid_until
    report, digest, signer, fresh = s.oracle.call("latestDiagnostic")
    assert (
        fresh and digest.hex() == s.signed.digest and to_checksum_address(signer) == FIRST.address
    )
    assert dict(zip((name for name, _ in REPORT_FIELDS), report, strict=True))["status"] == 0
    assert s.oracle.call("lastRound") == (OBSERVED,)
    assert s.oracle.call("usedAssessmentIds", s.report["assessmentId"]) == (True,)
    assert s.consumer.call("canAct") == (False,)
    s.consumer.rejects("readPrice", error="FeedUnavailable()")


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("feedId", "0x" + "a" * 64, "InvalidContext()"),
        ("datasetId", "0x" + "b" * 64, "InvalidContext()"),
        ("policyId", "0x" + "c" * 64, "InvalidContext()"),
        ("configurationHash", "0x" + "d" * 64, "InvalidContext()"),
        ("assessmentId", "0x" + "0" * 64, "InvalidContext()"),
        ("evidenceRoot", "0x" + "0" * 64, "InvalidContext()"),
        ("schemaVersion", 2, "UnsupportedReport()"),
        ("decimals", 8, "UnsupportedReport()"),
        ("status", 1, "UnsupportedReport()"),
        ("value", 100, "UnsupportedReport()"),
        ("confidenceBps", 9700, "UnsupportedReport()"),
        ("publicationReady", True, "UnsupportedReport()"),
        ("roundId", OBSERVED + 1, "InvalidTimes()"),
        ("observedAt", OBSERVED + 10, "InvalidTimes()"),
        ("validUntil", OBSERVED + 31, "InvalidTimes()"),
        ("issuedAt", OBSERVED + 30, "InvalidTimes()"),
        ("keyId", "0x" + "e" * 64, "SignerInactive()"),
    ],
)
def test_report_context_and_unavailable_schema_are_enforced(scenario, field, value, error):
    s = scenario
    s.initialize()
    s.warp(OBSERVED + 2)
    changed = {**s.report, field: value}
    data = typed_data(s.policy.domain, s.signed.message)
    data["message"] = changed
    signature = "0x" + bytes(FIRST.sign_typed_data(full_message=data).signature).hex()
    s.oracle.rejects("submit", changed, signature, error=error)
    assert s.oracle.call("lastRound") == (0,)


def test_replay_same_round_and_old_assessment_cannot_advance_state(scenario):
    s = scenario
    s.initialize()
    s.warp(OBSERVED + 1)
    s.oracle.tx("submit", s.report, s.signature)
    s.oracle.rejects("submit", s.report, s.signature, error="ReplayedReport()")
    newer = s.sign(bundle=s.bundles[1], issued_at=OBSERVED + 11)
    projection = typed_data(newer.domain, newer.message)["message"]
    s.warp(OBSERVED + 11)
    s.oracle.tx("submit", projection, newer.signature, sender=s.relayer)
    s.oracle.rejects("submit", s.report, s.signature, error="ReplayedReport()")
    # A compromised publisher cannot reuse the old assessment under a larger round.
    changed = s.signed.message.model_copy(
        update={"observed_at": OBSERVED + 12, "round_id": OBSERVED + 12, "issued_at": OBSERVED + 12}
    )
    signature = "0x" + bytes(FIRST.sign_message(signable(s.policy.domain, changed)).signature).hex()
    s.warp(OBSERVED + 12)
    s.oracle.rejects(
        "submit",
        typed_data(s.policy.domain, changed)["message"],
        signature,
        error="ReplayedReport()",
    )
    assert s.oracle.call("lastRound") == (OBSERVED + 10,)


@pytest.mark.parametrize("offset,valid", [(0, False), (1, True), (29, True), (30, False)])
def test_block_time_expiry_boundaries(scenario, offset, valid):
    s = scenario
    s.initialize()
    s.warp(OBSERVED + offset)
    if valid:
        s.oracle.call("submit", s.report, s.signature)
    else:
        s.oracle.rejects("submit", s.report, s.signature, error="ReportExpired()")


def test_expired_stored_diagnostics_never_become_consumer_prices(scenario):
    s = scenario
    s.initialize()
    s.warp(OBSERVED + 1)
    s.oracle.tx("submit", s.report, s.signature)
    assert s.oracle.call("latestDiagnostic")[-1]
    s.warp(OBSERVED + 30)
    assert not s.oracle.call("latestDiagnostic")[-1]
    assert s.consumer.call("canAct") == (False,)
    s.consumer.rejects("readPrice", error="FeedUnavailable()")


@pytest.mark.parametrize("kind", ["foreign", "short", "compact", "high-s", "bad-v", "zero"])
def test_signature_authentication_and_canonical_encoding(scenario, kind):
    s = scenario
    s.initialize()
    s.warp(OBSERVED + 2)
    raw = bytes.fromhex(s.signature[2:])
    if kind == "foreign":
        raw = bytes(SECOND.sign_message(signable(s.policy.domain, s.signed.message)).signature)
    elif kind == "short":
        raw = b"short"
    elif kind == "compact":
        raw = raw[:64]
    elif kind == "high-s":
        raw = (
            raw[:32]
            + (SECP256K1_N - int.from_bytes(raw[32:64], "big")).to_bytes(32, "big")
            + bytes([55 - raw[64]])
        )
    elif kind == "bad-v":
        raw = raw[:64] + b"\x00"
    else:
        raw = b"\x00" * 64 + b"\x1b"
    s.oracle.rejects("submit", s.report, "0x" + raw.hex())
    assert s.oracle.call("lastRound") == (0,)


@pytest.mark.parametrize("changed", ["chain", "contract", "evidenceRoot", "assessmentId"])
def test_signatures_cannot_cross_domains_or_committed_evidence(scenario, changed):
    s = scenario
    s.initialize()
    s.warp(OBSERVED + 2)
    domain, report = s.policy.domain, s.report
    if changed == "chain":
        domain = domain.model_copy(update={"chain_id": domain.chain_id + 1})
    elif changed == "contract":
        clone = Contract(s.tester, "DiagnosticOracle", (s.owner, keccak(text=DATASET)))
        domain = domain.model_copy(update={"verifying_contract": clone.address})
    else:
        report = {**s.report, changed: "0x" + "f" * 64}
    signature = s.signature
    if changed in ("chain", "contract"):
        assert report_digest(domain, s.signed.message) != s.signed.digest
        signature = (
            "0x" + bytes(FIRST.sign_message(signable(domain, s.signed.message)).signature).hex()
        )
    s.oracle.rejects("submit", report, signature, error="UnauthorizedSigner()")


@pytest.mark.parametrize("ttl,age", [(5, 30), (30, 5)])
def test_onchain_time_limits_cannot_be_extended_by_the_publisher(scenario, ttl, age):
    s = scenario
    p = s.payload
    s.oracle.tx(
        "initialize",
        p["policy_id"],
        p["configuration_hash"],
        ttl,
        age,
        p["registry_id"],
        p["grants"],
    )
    s.warp(OBSERVED + 2)
    s.oracle.rejects("submit", s.report, s.signature, error="InvalidTimes()")


def test_administration_uses_two_step_ownership_and_no_renunciation(scenario):
    s = scenario
    p = s.payload
    initialization = (
        p["policy_id"],
        p["configuration_hash"],
        30,
        30,
        p["registry_id"],
        p["grants"],
    )
    s.oracle.rejects(
        "initialize",
        *initialization,
        sender=s.attacker,
        error="OwnableUnauthorizedAccount(address)",
    )
    s.initialize()
    s.oracle.rejects("initialize", *initialization, error="AlreadyInitialized()")
    s.oracle.rejects(
        "revokeSigner",
        s.report["keyId"],
        sender=s.attacker,
        error="OwnableUnauthorizedAccount(address)",
    )
    s.oracle.rejects(
        "applyRegistry",
        p["registry_id"],
        "0x" + "a" * 64,
        2,
        p["grants"],
        sender=s.attacker,
        error="OwnableUnauthorizedAccount(address)",
    )
    s.oracle.tx("transferOwnership", s.relayer)
    assert to_checksum_address(s.oracle.call("owner")[0]) == s.owner
    s.oracle.rejects(
        "acceptOwnership", sender=s.attacker, error="OwnableUnauthorizedAccount(address)"
    )
    s.oracle.tx("acceptOwnership", sender=s.relayer)
    assert to_checksum_address(s.oracle.call("owner")[0]) == s.relayer
    s.oracle.rejects("revokeSigner", s.report["keyId"], error="OwnableUnauthorizedAccount(address)")
    s.oracle.tx("revokeSigner", s.report["keyId"], sender=s.relayer)
    s.oracle.rejects("renounceOwnership", sender=s.relayer, error="RenunciationDisabled()")


def test_rotation_activation_retirement_and_replay_across_registry_versions(scenario):
    s = scenario
    s.initialize()
    rotated = rotate_registry(
        s.registry,
        old_key_id="first",
        new_key_id="second",
        new_signer=SECOND.address,
        activate_at=OBSERVED + 5,
        retire_at=OBSERVED + 30,
        valid_until=OBSERVED + 3600,
    )
    s.apply(rotated)
    assert s.oracle.call("registryRevision") == (2,)
    s.warp(OBSERVED + 4)
    # Properly signed malicious early use of the next key, bypassing SDK authorization checks.
    data = typed_data(s.policy.domain, s.signed.message)
    data["message"] = {
        **s.report,
        "keyId": "0x" + keccak(text="second").hex(),
        "issuedAt": OBSERVED + 4,
    }
    signature = "0x" + bytes(SECOND.sign_typed_data(full_message=data).signature).hex()
    s.oracle.rejects("submit", data["message"], signature, error="SignerInactive()")
    new = s.sign(registry=rotated, key_id="second", account=SECOND, issued_at=OBSERVED + 5)
    s.warp(OBSERVED + 5)
    s.oracle.call("submit", s.report, s.signature)  # Old key is still authorized during overlap.
    new_report = typed_data(new.domain, new.message)["message"]
    s.oracle.tx("submit", new_report, new.signature, sender=s.relayer)
    s.oracle.rejects("submit", s.report, s.signature, error="ReplayedReport()")
    s.warp(OBSERVED + 31)
    stale_registry_report = s.sign(bundle=s.bundles[1], issued_at=OBSERVED + 31)
    s.oracle.rejects(
        "submit",
        typed_data(stale_registry_report.domain, stale_registry_report.message)["message"],
        stale_registry_report.signature,
        error="SignerInactive()",
    )


def test_registry_revocation_boundary_rejects_previously_issued_reports(scenario):
    s = scenario
    s.initialize()
    revoked = revoke_registry(s.registry, key_id="first", at=OBSERVED + 4)
    s.apply(revoked)
    s.warp(OBSERVED + 1)
    s.oracle.tx("submit", s.report, s.signature)
    assert s.oracle.call("latestDiagnostic")[-1]
    s.warp(OBSERVED + 4)
    assert not s.oracle.call("latestDiagnostic")[-1]
    s.oracle.rejects("submit", s.report, s.signature, error="SignerInactive()")
    s.oracle.rejects(
        "applyRegistry",
        "0x" + revoked.registry_id,
        "0x" + "a" * 64,
        3,
        registry_grants(s.registry),
        error="AuthorizationExtended()",
    )


def test_zero_time_revocation_is_distinct_from_no_revocation(scenario):
    s = scenario
    s.initialize()
    revoked = revoke_registry(s.registry, key_id="first", at=0)
    projection = registry_inputs(
        policy=s.policy, registry=revoked, expected_registry_id=revoked.registry_id
    )
    assert projection["grants"][0]["revoked"] and projection["grants"][0]["revokedAt"] == 0
    s.apply(revoked)
    s.warp(OBSERVED + 1)
    s.oracle.rejects("submit", s.report, s.signature, error="SignerInactive()")


def test_emergency_revocation_persists_across_registry_updates(scenario):
    s = scenario
    s.initialize()
    s.warp(OBSERVED + 1)
    s.oracle.tx("submit", s.report, s.signature)
    s.oracle.tx("revokeSigner", s.report["keyId"])
    at, active = s.oracle.call("emergencyRevocations", s.report["keyId"])
    assert active and at == OBSERVED + 3
    assert not s.oracle.call("latestDiagnostic")[-1]
    rotated = rotate_registry(
        s.registry,
        old_key_id="first",
        new_key_id="second",
        new_signer=SECOND.address,
        activate_at=OBSERVED + 5,
        retire_at=OBSERVED + 30,
        valid_until=OBSERVED + 3600,
    )
    s.apply(rotated)
    s.oracle.tx("revokeSigner", s.report["keyId"])
    assert s.oracle.call("emergencyRevocations", s.report["keyId"]) == (at, True)
    s.oracle.rejects("submit", s.report, s.signature, error="SignerInactive()")
    assert s.consumer.call("canAct") == (False,)


@pytest.mark.parametrize(
    "kind",
    [
        "predecessor",
        "revision",
        "reused-id",
        "empty",
        "duplicate",
        "oversized",
        "address-reuse",
        "reassign",
        "activation",
        "extend",
        "window",
        "revocation-shape",
        "remove-old",
    ],
)
def test_invalid_registry_successors_are_rejected(scenario, kind):
    s = scenario
    s.initialize()
    previous, new_id, revision = s.payload["registry_id"], "0x" + "a" * 64, 2
    entries = [dict(entry) for entry in s.payload["grants"]]
    if kind == "predecessor":
        previous = "0x" + "0" * 64
    elif kind == "revision":
        revision = 4
    elif kind == "reused-id":
        new_id = previous
    elif kind == "empty":
        entries = []
    elif kind == "duplicate":
        entries *= 2
    elif kind == "oversized":
        entries *= 129
    elif kind == "address-reuse":
        entries.append({**entries[0], "keyId": "0x" + "b" * 64})
    elif kind == "reassign":
        entries[0]["signer"] = SECOND.address
    elif kind == "activation":
        entries[0]["validFrom"] += 1
    elif kind == "extend":
        entries[0]["validUntil"] += 1
    elif kind == "window":
        entries[0]["validUntil"] = entries[0]["validFrom"]
    elif kind == "revocation-shape":
        entries[0]["revokedAt"] = OBSERVED
    else:
        entries = [{**entries[0], "keyId": "0x" + "b" * 64, "signer": SECOND.address}]
    entries.sort(key=lambda item: item["keyId"])
    s.oracle.rejects("applyRegistry", previous, new_id, revision, entries)
    assert s.oracle.call("registryId")[0].hex() == s.registry.registry_id
    assert s.oracle.call("registryRevision") == (1,)


def test_failed_registry_and_report_transactions_roll_back_all_effects(scenario):
    s = scenario
    s.initialize()
    original = s.payload["grants"][0]
    new_key = next(
        "0x" + keccak(text=f"next-{i}").hex()
        for i in range(100)
        if keccak(text=f"next-{i}").hex() < original["keyId"][2:]
    )
    entries = [
        {**original, "keyId": new_key, "signer": SECOND.address},
        {**original, "validUntil": original["validUntil"] + 1},
    ]
    failed_id = "0x" + "a" * 64
    s.oracle.tx("applyRegistry", s.payload["registry_id"], failed_id, 2, entries, succeeds=False)
    assert s.oracle.call("grants", new_key)[0] == b"\x00" * 32
    assert s.oracle.call("usedRegistryIds", failed_id) == (False,)
    assert s.oracle.call("registryRevision") == (1,)
    s.warp(OBSERVED + 1)
    s.oracle.tx("submit", {**s.report, "value": 100}, s.signature, succeeds=False)
    assert s.oracle.call("lastRound") == (0,)
    assert s.oracle.call("usedAssessmentIds", s.report["assessmentId"]) == (False,)
    s.oracle.tx("submit", s.report, s.signature)
    assert s.oracle.call("lastRound") == (OBSERVED,)


def test_contract_input_cli_is_offline_and_preserves_revoked_registry_export(
    scenario, tmp_path, monkeypatch, capsys
):
    from verdict.cli import main

    s = scenario
    files = {
        name: tmp_path / f"{name}.json"
        for name in (
            "report",
            "policy",
            "registry",
            "bundle",
            "output",
            "revoked",
            "registry-output",
        )
    }
    for name, model in (
        ("report", s.signed),
        ("policy", s.policy),
        ("registry", s.registry),
        ("bundle", s.bundles[0]),
    ):
        write_new_json(files[name], model.model_dump(mode="json"))
    monkeypatch.setattr(
        "verdict.cli.build_engine", lambda *args: pytest.fail("ABI export accessed SQL")
    )
    arguments = [
        "verdict",
        "signing",
        "contract-input",
        "--report",
        str(files["report"]),
        "--policy",
        str(files["policy"]),
        "--registry",
        str(files["registry"]),
        "--bundle",
        str(files["bundle"]),
        "--registry-id",
        s.registry.registry_id,
        "--at",
        datetime.fromtimestamp(OBSERVED + 1, UTC).isoformat(),
        "--output",
        str(files["output"]),
    ]
    monkeypatch.setattr("sys.argv", arguments)
    main()
    capsys.readouterr()
    value = json.loads(files["output"].read_text())
    assert value["digest"] == "0x" + s.signed.digest
    assert value["report"] == s.report
    with pytest.raises(SystemExit):
        main()
    revoked = revoke_registry(s.registry, key_id="first", at=0)
    write_new_json(files["revoked"], revoked.model_dump(mode="json"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "signing",
            "registry-input",
            "--policy",
            str(files["policy"]),
            "--registry",
            str(files["revoked"]),
            "--registry-id",
            revoked.registry_id,
            "--output",
            str(files["registry-output"]),
        ],
    )
    main()
    assert json.loads(files["registry-output"].read_text())["grants"][0]["revoked"]
    with pytest.raises(ValueError, match="trusted registry"):
        registry_inputs(policy=s.policy, registry=s.registry, expected_registry_id="0" * 64)


def test_maximum_registry_size_fits_the_local_block_gas_limit(scenario):
    s = scenario
    keys = [s.registry.keys[0]]
    for index in range(127):
        keys.append(
            SignerAuthorization(
                key_id=f"spare-{index:03d}",
                signer=to_checksum_address((index + 1000).to_bytes(20, "big")),
                valid_from=OBSERVED - 60,
                valid_until=OBSERVED + 3600,
            )
        )
    registry = s.registry.model_copy(update={"keys": tuple(keys)})
    inputs = registry_inputs(
        policy=s.policy, registry=registry, expected_registry_id=registry.registry_id
    )
    p = s.payload
    receipt = s.oracle.tx(
        "initialize",
        p["policy_id"],
        p["configuration_hash"],
        30,
        30,
        inputs["registry_id"],
        inputs["grants"],
        gas=25_000_000,
    )
    assert receipt["gas_used"] < 25_000_000
    for key in inputs["grants"]:
        assert to_checksum_address(s.oracle.call("grants", key["keyId"])[1]) == key["signer"]
