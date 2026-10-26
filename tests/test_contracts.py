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
from verdict.contract_inputs import contract_inputs
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
OBSERVED = unix_seconds(BASE) + 290
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

    def tx(self, name, *arguments, sender=None, succeeds=True):
        receipt = self.tester.get_transaction_receipt(
            self.tester.send_transaction(self.transaction(name, *arguments, sender=sender))
        )
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
            history = store.confidence_rounds(dataset=DATASET, limit=2)
            result = [
                build_bundle(store, dataset=DATASET, assessment_id=row["report_id"])
                for row in reversed(history)
            ]
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
        return self.oracle.tx(
            "applyRegistry",
            "0x" + registry.previous_registry_id,
            "0x" + registry.registry_id,
            registry.revision,
            registry_grants(registry),
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
    assert receipt["logs"]
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
    s.oracle.rejects("submit", changed, s.signature, error=error)
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
