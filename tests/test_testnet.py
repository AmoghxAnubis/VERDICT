"""Reviewed unsigned plans, read-only RPC boundaries, and actual local-EVM receipts."""

import json
from copy import deepcopy

import httpx
import pytest
from eth_tester import EthereumTester, PyEVMBackend
from eth_tester.exceptions import TransactionNotFound
from test_contracts import DATASET, FIRST, OBSERVED, Contract
from test_contracts import bundles as bundles  # Reuse independently verified retained evidence.

from verdict.contract_artifacts import load_artifact, runtime_matches
from verdict.dependencies import checksum
from verdict.signing import (
    ReportDomain,
    ReportPolicy,
    SignerAuthorization,
    SignerRegistry,
    sign_report,
)
from verdict.testnet import (
    NetworkProfile,
    TransactionPlan,
    deployment_plan,
    initialization_plan,
    publication_decision,
    publication_plan,
    validate_plan,
)
from verdict.testnet_cli import DEFAULT_PROFILE
from verdict.testnet_rpc import READ_METHODS, ReadRPC, audit_receipt, preflight, quantity


def profile(**changes):
    value = json.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
    return NetworkProfile.model_validate({**value, **changes})


def rehash(plan, mutate):
    value = plan.model_dump(mode="json", by_alias=True, exclude={"plan_id"})
    mutate(value)
    return TransactionPlan(**value, plan_id=checksum(value))


class LocalRPC:
    """Test-only adapter. Production has no path to these unlocked fake accounts."""

    def __init__(self, tester):
        self.tester, self.methods, self.finalized = tester, [], None

    def tag(self, value):
        if value == "finalized":
            if self.finalized is None:
                raise ValueError("finalized tag unsupported")
            return self.finalized
        return int(value, 16) if value.startswith("0x") else value

    def transaction(self, value):
        names = {
            "maxFeePerGas": "max_fee_per_gas",
            "maxPriorityFeePerGas": "max_priority_fee_per_gas",
        }
        return {
            names.get(key, key): int(item, 16)
            if key in {"nonce", "gas", "value", "maxFeePerGas", "maxPriorityFeePerGas"}
            else item
            for key, item in value.items()
            if key not in {"type", "chainId"}
        }

    def request(self, method, params):
        assert method in READ_METHODS
        self.methods.append(method)
        t = self.tester
        if method == "eth_chainId":
            return hex(t.backend.chain.chain_id)
        if method == "eth_getBlockByNumber":
            b = t.get_block_by_number(self.tag(params[0]))
            return {
                "number": hex(b["number"]),
                "hash": b["hash"],
                "timestamp": hex(b["timestamp"]),
                "gasLimit": hex(b["gas_limit"]),
                "baseFeePerGas": hex(b["base_fee_per_gas"]),
            }
        if method == "eth_getBalance":
            return hex(t.get_balance(params[0], self.tag(params[1])))
        if method == "eth_getTransactionCount":
            return hex(t.get_nonce(params[0], self.tag(params[1])))
        if method == "eth_getCode":
            return t.get_code(params[0], self.tag(params[1]))
        if method in {"eth_call", "eth_estimateGas"}:
            tx = self.transaction(params[0])
            tx.setdefault("from", t.get_accounts()[0])
            at = self.tag(params[1])
            return t.call(tx, at) if method == "eth_call" else hex(t.estimate_gas(tx, at))
        try:
            if method == "eth_getTransactionByHash":
                tx = t.get_transaction_by_hash(params[0])
                return {
                    "hash": tx["hash"],
                    "from": tx["from"],
                    "to": tx["to"] or None,
                    "input": tx["data"],
                    "type": tx["type"],
                    "blockHash": tx["block_hash"],
                    "blockNumber": hex(tx["block_number"])
                    if tx["block_number"] is not None
                    else None,
                    **{
                        new: hex(tx[old])
                        for old, new in {
                            "nonce": "nonce",
                            "value": "value",
                            "gas": "gas",
                            "chain_id": "chainId",
                            "max_fee_per_gas": "maxFeePerGas",
                            "max_priority_fee_per_gas": "maxPriorityFeePerGas",
                        }.items()
                    },
                }
            r = t.get_transaction_receipt(params[0])
            return {
                "transactionHash": r["transaction_hash"],
                "blockHash": r["block_hash"],
                "blockNumber": hex(r["block_number"]),
                "from": r["from"],
                "to": r["to"] or None,
                "status": hex(r["status"]),
                "gasUsed": hex(r["gas_used"]),
                "contractAddress": r["contract_address"],
                "logs": [
                    {"address": log["address"], "topics": log["topics"], "data": log["data"]}
                    for log in r["logs"]
                ],
            }
        except TransactionNotFound:
            return None

    def send_in_test(self, plan):
        tx = plan.transaction.model_dump(mode="json", by_alias=True, exclude_none=True)
        tx.pop("chainId")
        tx.pop("type")
        tx["max_fee_per_gas"] = tx.pop("maxFeePerGas")
        tx["max_priority_fee_per_gas"] = tx.pop("maxPriorityFeePerGas")
        return self.tester.send_transaction(tx)


class Harness:
    def __init__(self):
        self.tester = EthereumTester(
            PyEVMBackend(
                genesis_parameters={
                    "timestamp": OBSERVED - 10,
                    "gas_limit": 30_000_000,
                }
            )
        )
        self.tester.backend.chain.chain_id = 11155111
        self.rpc = LocalRPC(self.tester)
        self.profile = profile(
            genesis_hash=self.tester.get_block_by_number(0)["hash"],
            dataset=DATASET,
            publication_interval_seconds=10,
        )
        self.sender, self.admin, self.relayer = self.tester.get_accounts()[:3]

    @property
    def now(self):
        return self.tester.get_block_by_number("latest")["timestamp"]

    def deploy(self, **changes):
        return deployment_plan(
            self.profile,
            operation="deploy-oracle",
            sender=self.sender,
            administrator=self.admin,
            nonce=0,
            now=self.now,
            **changes,
        )

    def preflight(self, plan):
        return preflight(plan, self.profile, self.rpc, expected_plan_id=plan.plan_id, now=self.now)

    def audit(self, plan, tx_hash):
        return audit_receipt(
            plan,
            self.profile,
            self.rpc,
            expected_plan_id=plan.plan_id,
            transaction_hash=tx_hash,
            now=self.now,
        )

    def configured(self, bundle):
        deploy = self.deploy()
        tx_hash = self.rpc.send_in_test(deploy)
        self.oracle = deploy.expected_contract_address
        assert self.audit(deploy, tx_hash)["status"] == "included"
        self.policy = ReportPolicy(
            version=self.profile.version,
            dataset=DATASET,
            domain=ReportDomain(chain_id=11155111, verifying_contract=self.oracle),
            maximum_ttl_seconds=120,
            maximum_observation_age_seconds=120,
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
        plan = initialization_plan(
            self.profile,
            sender=self.admin,
            nonce=0,
            oracle=self.oracle,
            bundle=bundle,
            policy=self.policy,
            registry=self.registry,
            expected_registry_id=self.registry.registry_id,
            now=self.now,
        )
        assert self.preflight(plan)["ready"]
        tx_hash = self.rpc.send_in_test(plan)
        assert self.audit(plan, tx_hash)["verified"]

    def publish(self, bundle):
        self.tester.time_travel(OBSERVED + 2)
        self.tester.mine_blocks()
        signed = sign_report(
            bundle,
            policy=self.policy,
            registry=self.registry,
            key_id="first",
            account=FIRST,
            issued_at=self.now,
        )
        return publication_plan(
            self.profile,
            sender=self.relayer,
            nonce=0,
            oracle=self.oracle,
            signed=signed,
            bundle=bundle,
            policy=self.policy,
            registry=self.registry,
            expected_registry_id=self.registry.registry_id,
            now=self.now,
        )


@pytest.fixture
def chain():
    return Harness()


def test_profile_pins_compiled_artifacts():
    p = profile()
    assert p.chain_id == 11155111
    assert checksum(load_artifact("DiagnosticOracle")) == p.oracle_artifact_id
    assert checksum(load_artifact("FailClosedConsumer")) == p.consumer_artifact_id


@pytest.mark.parametrize(
    "changes",
    [
        {"chain_id": 1},
        {"dataset": "live"},
        {"publication_interval_seconds": 11},
        {"minimum_submission_remaining_seconds": 10},
        {"maximum_initial_signers": 9},
        {"maximum_gas_limit": 16_777_216},
        {"maximum_ttl_seconds": 40},
        {"maximum_priority_fee_per_gas_wei": 30_000_000_000},
    ],
)
def test_profile_rejects_incompatible_bounds(changes):
    with pytest.raises(ValueError):
        profile(**changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("data", "0x"),
        ("gas", 16_700_001),
        ("value", 1),
        ("nonce", -1),
        ("chainId", 1),
        ("maxFeePerGas", 21_000_000_000),
        ("maxPriorityFeePerGas", 3_000_000_000),
    ],
)
def test_plan_rejects_rehashed_transaction_changes(chain, field, value):
    plan = chain.deploy()
    with pytest.raises(ValueError):
        changed = rehash(plan, lambda p: p["transaction"].update({field: value}))
        validate_plan(changed, chain.profile)


def test_plan_identity_expiry_and_reviewed_id(chain):
    plan = chain.deploy()
    value = plan.model_dump(mode="json", by_alias=True)
    value["created_at"] += 1
    with pytest.raises(ValueError, match="checksum"):
        TransactionPlan.model_validate(value)
    for now in (plan.created_at - 1, plan.expires_at):
        with pytest.raises(ValueError, match="expired"):
            validate_plan(plan, chain.profile, now=now)
    with pytest.raises(ValueError, match="reviewed"):
        validate_plan(plan, chain.profile, expected_plan_id="0" * 64)


@pytest.mark.parametrize(
    "now,cutoff,last_round,pending,expected",
    [
        (122, 120, 90, False, "ready-for-verified-plan"),
        (122, 120, 90, True, "wait-for-pending-transaction"),
        (122, 120, 120, False, "skip-already-published"),
        (122, 110, 90, False, "wait-for-round-boundary"),
        (196, 120, 90, False, "skip-expiring-evidence"),
        (122, 120, 110, False, "wait-for-publication-cadence"),
        (122, 150, 90, False, "wait-for-round-boundary"),
    ],
)
def test_publication_cadence(now, cutoff, last_round, pending, expected):
    assert (
        publication_decision(
            profile(), now=now, cutoff=cutoff, last_round=last_round, in_flight=pending
        )
        == expected
    )


@pytest.mark.parametrize("value", [True, 1, None, "0x00", "0x01", "0x-1", "0xA", "0x" + "f" * 65])
def test_rpc_quantities_are_canonical(value):
    with pytest.raises(ValueError):
        quantity(value)


@pytest.mark.parametrize(
    "response",
    [
        {"jsonrpc": "2.0", "id": 1, "error": {"message": "secret-token"}},
        {"jsonrpc": "2.0", "id": True, "result": "0x1"},
        {"jsonrpc": "2.0", "id": 2, "result": "0x1"},
        [],
    ],
)
def test_rpc_errors_do_not_echo_provider_credentials(response):
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))
    ) as client:
        rpc = ReadRPC(client, "https://rpc.example/secret-token")
        with pytest.raises(ValueError) as error:
            rpc.request("eth_chainId", [])
        assert "secret-token" not in str(error.value)
        with pytest.raises(ValueError, match="allowlist"):
            rpc.request("eth_sendRawTransaction", ["0x"])


def test_rpc_full_http_preflight_is_read_only(chain):
    def handle(request):
        value = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": value["id"],
                "result": chain.rpc.request(value["method"], value["params"]),
            },
        )

    plan = chain.deploy()
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = preflight(
            plan,
            chain.profile,
            ReadRPC(client, "https://fake.example/token"),
            expected_plan_id=plan.plan_id,
            now=chain.now,
        )
    assert result["ready"] and result["broadcast"] is False
    assert "token" not in json.dumps(result)
    assert set(chain.rpc.methods) <= READ_METHODS
    assert chain.tester.get_nonce(chain.sender) == 0


@pytest.mark.parametrize(
    "fault", ["chain", "genesis", "pending", "balance", "base-fee", "gas", "reorg", "stale"]
)
def test_preflight_rejects_network_and_execution_races(chain, monkeypatch, fault):
    plan = chain.deploy()
    original = chain.rpc.request
    latest_seen = False

    def request(method, params):
        nonlocal latest_seen
        value = original(method, params)
        if fault == "chain" and method == "eth_chainId":
            return "0x1"
        if fault == "genesis" and method == "eth_getBlockByNumber" and params[0] == "0x0":
            return {**value, "hash": "0x" + "00" * 32}
        if fault == "pending" and method == "eth_getTransactionCount" and params[1] == "pending":
            return "0x1"
        if fault == "balance" and method == "eth_getBalance":
            return "0x0"
        if fault == "base-fee" and method == "eth_getBlockByNumber":
            return {**value, "baseFeePerGas": hex(21_000_000_000)}
        if fault == "gas" and method == "eth_estimateGas":
            return hex(plan.transaction.gas)
        if method == "eth_getBlockByNumber":
            if params[0] == "latest":
                latest_seen = True
                if fault == "stale":
                    return {**value, "timestamp": hex(chain.now - 61)}
            elif fault == "reorg" and latest_seen:
                return {**value, "hash": "0x" + "00" * 32}
        return value

    monkeypatch.setattr(chain.rpc, "request", request)
    with pytest.raises(ValueError):
        chain.preflight(plan)


def test_deployment_initialization_publication_and_consumer_receipts(chain, bundles):
    chain.configured(bundles[0])
    consumer = deployment_plan(
        chain.profile,
        operation="deploy-consumer",
        sender=chain.sender,
        nonce=1,
        oracle=chain.oracle,
        now=chain.now,
    )
    assert chain.preflight(consumer)["ready"]
    tx_hash = chain.rpc.send_in_test(consumer)
    assert chain.audit(consumer, tx_hash)["status"] == "included"
    deployed_code = chain.tester.get_code(consumer.expected_contract_address)
    assert runtime_matches(load_artifact("FailClosedConsumer"), deployed_code)
    assert not runtime_matches(load_artifact("FailClosedConsumer"), "0x00" + deployed_code[4:])
    publication = chain.publish(bundles[0])
    assert chain.preflight(publication)["ready"]
    tx_hash = chain.rpc.send_in_test(publication)
    assert chain.audit(publication, tx_hash)["status"] == "included"
    chain.tester.mine_blocks()
    assert chain.audit(publication, tx_hash)["status"] == "confirmed"
    chain.rpc.finalized = chain.tester.get_block_by_number("latest")["number"]
    assert chain.audit(publication, tx_hash)["status"] == "finalized"
    consumer_contract = Contract(
        chain.tester, "FailClosedConsumer", at=consumer.expected_contract_address
    )
    assert consumer_contract.call("canAct") == (False,)
    with pytest.raises(ValueError):
        chain.preflight(publication)


@pytest.mark.parametrize("fault", ["input", "nonce", "block", "runtime", "missing-event"])
def test_receipt_does_not_accept_different_transactions_or_forged_state(
    chain, bundles, monkeypatch, fault
):
    chain.configured(bundles[0])
    plan = chain.publish(bundles[0])
    tx_hash = chain.rpc.send_in_test(plan)
    original = chain.rpc.request

    def request(method, params):
        value = deepcopy(original(method, params))
        if method == "eth_getTransactionByHash" and fault in {"input", "nonce"}:
            value[fault] = "0x" if fault == "input" else "0x1"
        if method == "eth_getTransactionReceipt":
            if fault == "block":
                value["blockHash"] = "0x" + "00" * 32
            if fault == "missing-event":
                value["logs"] = []
        if method == "eth_getCode" and fault == "runtime":
            return "0x"
        return value

    monkeypatch.setattr(chain.rpc, "request", request)
    with pytest.raises(ValueError):
        chain.audit(plan, tx_hash)


def test_receipt_pending_and_not_found_are_not_confirmations(chain, monkeypatch):
    plan = chain.deploy()
    unknown = "0x" + "33" * 32
    assert chain.audit(plan, unknown)["status"] == "not-found"
    tx_hash = chain.rpc.send_in_test(plan)
    original = chain.rpc.request

    def request(method, params):
        value = original(method, params)
        if method == "eth_getTransactionReceipt":
            return None
        if method == "eth_getTransactionByHash":
            return {**value, "blockHash": None, "blockNumber": None}
        return value

    monkeypatch.setattr(chain.rpc, "request", request)
    assert chain.audit(plan, tx_hash)["status"] == "pending"


def test_reverted_receipt_is_never_verified(chain, bundles):
    chain.configured(bundles[0])
    # A wallet bypassing preflight can still mine a failing transaction. Audit must report it.
    repeated = initialization_plan(
        chain.profile,
        sender=chain.admin,
        nonce=1,
        oracle=chain.oracle,
        bundle=bundles[0],
        policy=chain.policy,
        registry=chain.registry,
        expected_registry_id=chain.registry.registry_id,
        now=chain.now,
    )
    with pytest.raises(ValueError, match="already initialized"):
        chain.preflight(repeated)
    tx_hash = chain.rpc.send_in_test(repeated)
    result = chain.audit(repeated, tx_hash)
    assert result["status"] == "reverted"
    assert result["verified"] is False


def test_nonce_is_checked_again_after_simulation(chain, monkeypatch):
    plan = chain.deploy()
    original = chain.rpc.request
    estimated = False

    def request(method, params):
        nonlocal estimated
        value = original(method, params)
        if method == "eth_estimateGas":
            estimated = True
        if estimated and method == "eth_getTransactionCount" and params[1] == "pending":
            return "0x1"
        return value

    monkeypatch.setattr(chain.rpc, "request", request)
    with pytest.raises(ValueError, match="nonce changed"):
        chain.preflight(plan)


def test_runtime_template_does_not_replace_immutable_domain_checks(chain, bundles, monkeypatch):
    from verdict.contract_artifacts import encode_arguments

    chain.configured(bundles[0])
    plan = chain.publish(bundles[0])
    original = chain.rpc.request
    selector = encode_arguments(load_artifact("DiagnosticOracle"), "datasetId", {})

    def request(method, params):
        if method == "eth_call" and params[0]["data"] == selector:
            return "0x" + "00" * 32
        return original(method, params)

    monkeypatch.setattr(chain.rpc, "request", request)
    with pytest.raises(ValueError, match="dataset"):
        chain.preflight(plan)


def test_cached_eip712_digest_is_checked_in_addition_to_getters(chain, bundles, monkeypatch):
    from verdict.contract_artifacts import encode_arguments

    chain.configured(bundles[0])
    plan = chain.publish(bundles[0])
    original = chain.rpc.request
    selector = encode_arguments(
        load_artifact("DiagnosticOracle"), "hashReport", {"report": plan.arguments["report"]}
    )[:10]

    def request(method, params):
        if method == "eth_call" and params[0]["data"].startswith(selector):
            return "0x" + "00" * 32
        return original(method, params)

    monkeypatch.setattr(chain.rpc, "request", request)
    with pytest.raises(ValueError, match="cached EIP-712"):
        chain.preflight(plan)


def test_cadence_rejects_invalid_clock_types():
    with pytest.raises(ValueError):
        publication_decision(profile(), now=True, cutoff=120, last_round=90)


@pytest.mark.parametrize("status", [302, 403, 429, 503])
def test_http_failures_and_redirects_hide_rpc_urls(status):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                status,
                headers={"Location": "https://other.example/secret-token"},
            )
        )
    ) as client:
        rpc = ReadRPC(client, "https://rpc.example/secret-token")
        with pytest.raises(ValueError) as error:
            rpc.request("eth_chainId", [])
        assert "secret-token" not in str(error.value)


def test_no_rpc_clock_override_and_no_broadcaster_in_cli(capsys, monkeypatch):
    from verdict.cli import main

    monkeypatch.setattr("sys.argv", ["verdict", "testnet", "preflight", "--help"])
    with pytest.raises(SystemExit):
        main()
    output = capsys.readouterr().out
    assert "--at" not in output
    assert "--private-key" not in output


def test_reviewed_cost_budget_is_checked(chain):
    smaller = NetworkProfile.model_validate(
        {
            **chain.profile.model_dump(mode="json"),
            "maximum_transaction_cost_wei": 1,
        }
    )
    with pytest.raises(ValueError, match="cost cap"):
        deployment_plan(
            smaller,
            operation="deploy-oracle",
            sender=chain.sender,
            administrator=chain.admin,
            nonce=0,
            now=chain.now,
        )


def test_cli_preparation_has_no_database_network_or_overwrite(tmp_path, monkeypatch, capsys):
    from verdict.cli import main

    monkeypatch.setenv("VERDICT_DATABASE_URL", "not-a-database")
    output = tmp_path / "plan.json"
    argv = [
        "verdict",
        "testnet",
        "plan-deploy",
        "--operation",
        "deploy-oracle",
        "--sender",
        "0x" + "11" * 20,
        "--administrator",
        "0x" + "22" * 20,
        "--nonce",
        "0",
        "--output",
        str(output),
    ]
    monkeypatch.setattr("sys.argv", argv)
    main()
    data = output.read_bytes()
    assert TransactionPlan.model_validate_json(data).operation == "deploy-oracle"
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert output.read_bytes() == data
    assert "broadcast" in capsys.readouterr().out
