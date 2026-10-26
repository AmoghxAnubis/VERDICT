"""Read-only RPC preflight and inclusion audits for reviewed Sepolia plans."""

import json
import re

import httpx
from eth_abi import decode, encode
from eth_utils import keccak
from pydantic import SecretStr, TypeAdapter

from verdict.contract_artifacts import (
    decode_result,
    encode_arguments,
    load_artifact,
    runtime_matches,
)
from verdict.dependencies import checksum
from verdict.signing import Uint64, address
from verdict.testnet import NetworkProfile, publication_decision, validate_plan

READ_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_getBlockByNumber",
        "eth_getBalance",
        "eth_getTransactionCount",
        "eth_getCode",
        "eth_call",
        "eth_estimateGas",
        "eth_getTransactionReceipt",
        "eth_getTransactionByHash",
    }
)


class CheckFailed(ValueError):
    """A public, credential-free explanation suitable for CLI output."""


class ReadRPC:
    """No transaction signing, account access, or broadcast methods are exposed."""

    def __init__(self, client: httpx.Client, url: str):
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            raise CheckFailed("invalid testnet RPC URL") from None
        if parsed.scheme != "https" or not parsed.host or parsed.username or parsed.password:
            raise CheckFailed("testnet RPC requires an HTTPS URL without user information")
        self.client, self._url, self._sequence = client, SecretStr(url), 0

    def request(self, method, params):
        if method not in READ_METHODS:
            raise CheckFailed("RPC method is outside the read-only allowlist")
        self._sequence += 1
        try:
            with self.client.stream(
                "POST",
                self._url.get_secret_value(),
                json={"jsonrpc": "2.0", "id": self._sequence, "method": method, "params": params},
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                body = bytearray()
                for part in response.iter_bytes():
                    body.extend(part)
                    if len(body) > 2_000_000:
                        raise ValueError("oversized RPC response")
                result = json.loads(body)
            if (
                not isinstance(result, dict)
                or result.get("jsonrpc") != "2.0"
                or type(result.get("id")) is not int
                or result["id"] != self._sequence
                or "error" in result
                or "result" not in result
            ):
                raise ValueError("invalid RPC envelope")
            return result["result"]
        except Exception:
            # Provider errors can contain credential-bearing URLs or submitted parameters.
            raise CheckFailed(f"read-only RPC request failed: {method}") from None


def quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-f][0-9a-f]{0,63})", value):
        raise ValueError("RPC returned a noncanonical unsigned quantity")
    return int(value, 16)


def hash32(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError("RPC returned an invalid hash")
    return value.lower()


def block(rpc, tag):
    value = rpc.request("eth_getBlockByNumber", [tag, False])
    if not isinstance(value, dict):
        raise ValueError("RPC block is unavailable")
    return {
        "number": quantity(value.get("number")),
        "hash": hash32(value.get("hash")),
        "timestamp": quantity(value.get("timestamp")),
        "gas_limit": quantity(value.get("gasLimit")),
        "base_fee": quantity(value.get("baseFeePerGas")),
    }


def network_head(rpc, profile, now):
    if quantity(rpc.request("eth_chainId", [])) != profile.chain_id:
        raise CheckFailed("RPC chain ID differs from the reviewed network")
    if block(rpc, "0x0")["hash"] != profile.genesis_hash.lower():
        raise CheckFailed("RPC genesis differs from the reviewed network")
    head = block(rpc, "latest")
    if not now - profile.maximum_rpc_lag_seconds <= head["timestamp"] <= now + 5:
        raise CheckFailed("RPC head is stale or ahead of the local clock")
    return head


def canonical(rpc, head):
    if block(rpc, hex(head["number"])) != head:
        raise CheckFailed("RPC block changed during verification; repeat the read-only check")


def rpc_transaction(tx):
    value = tx.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {key: hex(item) if type(item) is int else item for key, item in value.items()}


def call(rpc, artifact, target, name, args, tag):
    data = encode_arguments(artifact, name, args)
    return decode_result(
        artifact, name, rpc.request("eth_call", [{"to": target, "data": data}, tag])
    )


def oracle_context(rpc, profile, target, tag, *, owner=None):
    artifact = load_artifact("DiagnosticOracle", profile.oracle_artifact_id)
    if not runtime_matches(artifact, rpc.request("eth_getCode", [target, tag])):
        raise ValueError("oracle runtime differs from the pinned artifact")
    if call(rpc, artifact, target, "datasetId", {}, tag)[0] != keccak(text=profile.dataset):
        raise ValueError("oracle dataset differs from the selected profile")
    if call(rpc, artifact, target, "FEED_ID", {}, tag)[0] != keccak(text="ETH-USD"):
        raise ValueError("oracle feed mismatch")
    domain = call(rpc, artifact, target, "eip712Domain", {}, tag)
    if domain != (
        b"\x0f",
        "VERDICT Diagnostic Reports",
        "1",
        profile.chain_id,
        target.lower(),
        b"\x00" * 32,
        (),
    ):
        raise ValueError("oracle EIP-712 domain mismatch")
    if owner and address(call(rpc, artifact, target, "owner", {}, tag)[0]) != owner:
        raise ValueError("oracle administrator differs from the reviewed plan")
    return artifact


def pinned_configuration(rpc, artifact, plan, profile, tag):
    for name, expected in (
        ("initialized", True),
        ("policyId", bytes.fromhex(plan.policy_id)),
        ("configurationHash", bytes.fromhex(plan.configuration_hash)),
        ("registryId", bytes.fromhex(plan.registry_id)),
        ("maximumTtl", profile.maximum_ttl_seconds),
        ("maximumAge", profile.maximum_observation_age_seconds),
    ):
        if call(rpc, artifact, plan.oracle, name, {}, tag)[0] != expected:
            raise ValueError(f"on-chain {name} differs from the reviewed publication context")


def record(kind, plan, profile, now, **data):
    result = {
        "schema_version": 1,
        "kind": kind,
        "plan_id": plan.plan_id,
        "profile_id": profile.profile_id,
        "chain_id": profile.chain_id,
        "checked_at": now,
        **data,
    }
    return {**result, "record_id": checksum(result)}


def preflight(plan, profile, rpc, *, expected_plan_id, now):
    profile = NetworkProfile.model_validate(profile)
    plan = validate_plan(plan, profile, expected_plan_id=expected_plan_id, now=now)
    head = network_head(rpc, profile, now)
    tx, tag = plan.transaction, hex(head["number"])

    def check_nonce():
        nonces = [
            quantity(rpc.request("eth_getTransactionCount", [tx.sender, at]))
            for at in ("latest", "pending")
        ]
        if nonces != [tx.nonce, tx.nonce]:
            raise CheckFailed("sender nonce changed or another transaction is pending")

    check_nonce()
    worst_case = tx.gas * tx.maxFeePerGas
    if quantity(rpc.request("eth_getBalance", [tx.sender, "pending"])) < worst_case:
        raise CheckFailed("sender balance cannot cover the reviewed worst-case cost")
    if tx.gas > head["gas_limit"] or head["base_fee"] + tx.maxPriorityFeePerGas > tx.maxFeePerGas:
        raise CheckFailed("block gas limit or current base fee exceeds the plan")
    if plan.operation.startswith("deploy-"):
        if rpc.request("eth_getCode", [plan.expected_contract_address, tag]) != "0x":
            raise ValueError("predicted deployment address already contains code")
        if quantity(rpc.request("eth_getTransactionCount", [plan.expected_contract_address, tag])):
            raise ValueError("predicted deployment address already has a nonce")
        if plan.operation == "deploy-consumer":
            oracle_context(rpc, profile, plan.oracle, tag)
    else:
        artifact = oracle_context(rpc, profile, plan.oracle, tag, owner=plan.administrator)
        if plan.operation == "initialize":
            if call(rpc, artifact, plan.oracle, "initialized", {}, tag)[0]:
                raise ValueError("oracle is already initialized")
        else:
            pinned_configuration(rpc, artifact, plan, profile, tag)
            report = plan.arguments["report"]
            last_round = call(rpc, artifact, plan.oracle, "lastRound", {}, tag)[0]
            if (
                publication_decision(
                    profile, now=now, cutoff=report["roundId"], last_round=last_round
                )
                != "ready-for-verified-plan"
            ):
                raise ValueError("publication round violates cadence, freshness, or replay policy")
            if (
                not report["issuedAt"]
                <= head["timestamp"]
                < report["validUntil"] - profile.minimum_submission_remaining_seconds
            ):
                raise ValueError("chain clock leaves insufficient publication lifetime")
    request = rpc_transaction(tx)
    rpc.request("eth_call", [request, tag])
    estimate = quantity(rpc.request("eth_estimateGas", [request, tag]))
    if estimate < 21000 or (estimate * 110 + 99) // 100 > tx.gas:
        raise CheckFailed("gas estimate plus 10% margin exceeds the reviewed gas limit")
    check_nonce()
    canonical(rpc, head)
    return record(
        "preflight",
        plan,
        profile,
        now,
        ready=True,
        head=head,
        estimated_gas=estimate,
        gas_limit=tx.gas,
        worst_case_cost_wei=worst_case,
        expires_at=plan.expires_at,
        broadcast=False,
    )


def matching_transaction(actual, plan, tx_hash):
    if not isinstance(actual, dict) or hash32(actual.get("hash")) != tx_hash:
        raise ValueError("transaction lookup does not match the requested hash")
    expected = plan.transaction.model_dump(mode="json", by_alias=True, exclude_none=True)
    for name, value in expected.items():
        received = actual.get("input" if name == "data" else name)
        if type(value) is int:
            received = quantity(received)
        elif name in {"from", "to"}:
            received = address(received)
        if received != value:
            raise ValueError(f"included transaction {name} differs from the reviewed plan")
    if plan.transaction.to is None and actual.get("to") is not None:
        raise ValueError("deployment transaction has an unexpected recipient")
    if actual.get("accessList", []) != []:
        raise ValueError("included transaction has an unreviewed access list")


def matching_event(receipt, target, signature, topics, types, values):
    expected_topics = ["0x" + keccak(text=signature).hex(), *topics]
    for log in receipt.get("logs", []):
        if not isinstance(log, dict) or log.get("removed", False):
            continue
        if address(log["address"]) == target and log.get("topics") == expected_topics:
            raw = bytes.fromhex(log["data"][2:])
            actual = decode(types, raw, strict=True)
            if encode(types, actual) == raw and actual == values:
                return
    raise ValueError("receipt lacks the exact reviewed oracle event")


def audit_receipt(plan, profile, rpc, *, expected_plan_id, transaction_hash, now):
    # Expired plans remain auditable; only a fresh preflight may recommend sending.
    profile = NetworkProfile.model_validate(profile)
    now = TypeAdapter(Uint64).validate_python(now)
    plan = validate_plan(plan, profile, expected_plan_id=expected_plan_id)
    tx_hash = hash32(transaction_hash)
    head = network_head(rpc, profile, now)
    actual = rpc.request("eth_getTransactionByHash", [tx_hash])
    receipt = rpc.request("eth_getTransactionReceipt", [tx_hash])
    if actual is not None:
        matching_transaction(actual, plan, tx_hash)
    if receipt is None:
        if actual is not None and (
            actual.get("blockHash") is not None or actual.get("blockNumber") is not None
        ):
            raise ValueError("included transaction has no receipt; provider is inconsistent")
        canonical(rpc, head)
        return record(
            "receipt",
            plan,
            profile,
            now,
            transaction_hash=tx_hash,
            status="pending" if actual else "not-found",
            confirmations=0,
            head=head,
        )
    if (
        actual is None
        or not isinstance(receipt, dict)
        or hash32(receipt.get("transactionHash")) != tx_hash
    ):
        raise ValueError("receipt has no matching transaction")
    number = quantity(receipt.get("blockNumber"))
    included = block(rpc, hex(number))
    if (
        hash32(receipt.get("blockHash")) != included["hash"]
        or hash32(actual.get("blockHash")) != included["hash"]
        or quantity(actual.get("blockNumber")) != number
        or number > head["number"]
    ):
        raise ValueError("receipt is not on the observed canonical chain")
    if address(receipt.get("from")) != plan.transaction.sender or receipt.get("to") != actual.get(
        "to"
    ):
        raise ValueError("receipt sender/recipient mismatch")
    confirmations = head["number"] - number + 1
    status = quantity(receipt.get("status"))
    if status not in {0, 1}:
        raise ValueError("invalid transaction execution status")
    if quantity(receipt.get("gasUsed")) > plan.transaction.gas:
        raise ValueError("receipt gas exceeds the reviewed transaction limit")
    if status == 1:
        tag = hex(number)
        if plan.operation == "deploy-oracle":
            target = address(receipt.get("contractAddress"))
            if target != plan.expected_contract_address:
                raise ValueError("deployed address differs from the reviewed CREATE prediction")
            oracle_context(rpc, profile, target, tag, owner=plan.administrator)
        elif plan.operation == "deploy-consumer":
            target = address(receipt.get("contractAddress"))
            artifact = load_artifact("FailClosedConsumer", profile.consumer_artifact_id)
            if target != plan.expected_contract_address or not runtime_matches(
                artifact, rpc.request("eth_getCode", [target, tag])
            ):
                raise ValueError("consumer address or runtime mismatch")
            for name, expected in (
                ("oracle", plan.oracle.lower()),
                ("maximumAge", profile.consumer_maximum_age_seconds),
                ("minimumConfidenceBps", profile.minimum_confidence_bps),
                ("canAct", False),
            ):
                if call(rpc, artifact, target, name, {}, tag)[0] != expected:
                    raise ValueError("consumer constructor or fail-closed policy mismatch")
            oracle_context(rpc, profile, plan.oracle, tag)
        else:
            artifact = oracle_context(rpc, profile, plan.oracle, tag)
            if plan.operation == "initialize":
                pinned_configuration(rpc, artifact, plan, profile, tag)
                matching_event(
                    receipt,
                    plan.oracle,
                    "RegistryApplied(bytes32,bytes32,uint64)",
                    ["0x" + "00" * 32, "0x" + plan.registry_id],
                    ["uint64"],
                    (1,),
                )
            else:
                report = plan.arguments["report"]
                digest = call(rpc, artifact, plan.oracle, "hashReport", {"report": report}, tag)[0]
                signer = call(rpc, artifact, plan.oracle, "grants", {"": report["keyId"]}, tag)[1]
                matching_event(
                    receipt,
                    plan.oracle,
                    "DiagnosticAccepted(bytes32,bytes32,bytes32,uint64,address,uint64)",
                    [report["assessmentId"], report["evidenceRoot"], "0x" + digest.hex()],
                    ["uint64", "address", "uint64"],
                    (report["roundId"], signer, report["validUntil"]),
                )
    finalized = None
    try:
        finalized = block(rpc, "finalized")
    except ValueError:
        pass  # Some providers do not support finalized; inclusion never implies finality.
    is_finalized = finalized is not None and number <= finalized["number"] <= head["number"]
    if finalized:
        canonical(rpc, finalized)
    canonical(rpc, included)
    canonical(rpc, head)
    result_status = (
        "reverted"
        if status == 0
        else (
            "finalized"
            if is_finalized
            else "confirmed"
            if confirmations >= profile.confirmation_blocks
            else "included"
        )
    )
    return record(
        "receipt",
        plan,
        profile,
        now,
        transaction_hash=tx_hash,
        status=result_status,
        confirmations=confirmations,
        finalized=is_finalized,
        block=included,
        head=head,
        gas_used=quantity(receipt["gasUsed"]),
        verified=status == 1,
    )
