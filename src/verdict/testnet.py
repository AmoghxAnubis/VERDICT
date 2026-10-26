"""Sepolia diagnostic deployment plans. This module cannot sign or broadcast transactions."""

from typing import Annotated, Literal

import rlp
from eth_utils import keccak, to_checksum_address
from pydantic import ConfigDict, Field, TypeAdapter, field_validator, model_validator

from verdict.contract_artifacts import encode_arguments, load_artifact
from verdict.contract_inputs import contract_inputs, registry_inputs
from verdict.dependencies import FrozenModel, Identifier, Sha256, checksum
from verdict.evidence import EvidenceBundle, verify_bundle
from verdict.models import Address, Dataset, Hash32
from verdict.signing import (
    ReportPolicy,
    SignedDiagnosticReport,
    SignerRegistry,
    Uint64,
    address,
    configuration_hash,
)

Operation = Literal["deploy-oracle", "deploy-consumer", "initialize", "submit"]
HexData = Annotated[str, Field(pattern=r"^0x(?:[0-9a-f]{2})*$", max_length=100000)]
POSITIVE = Annotated[int, Field(ge=1, lt=2**256, strict=True)]
DEFAULT_GAS = {
    "deploy-oracle": 3_000_000,
    "deploy-consumer": 500_000,
    "initialize": 2_000_000,
    "submit": 750_000,
}


class NetworkProfile(FrozenModel):
    schema_version: Literal[1] = 1
    network: Literal["sepolia"] = "sepolia"
    version: Identifier
    chain_id: Literal[11155111] = 11155111
    genesis_hash: Hash32
    rpc_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,95}$")]
    dataset: Dataset
    oracle_artifact_id: Sha256
    consumer_artifact_id: Sha256
    publication_interval_seconds: int = Field(default=30, ge=10, le=300, strict=True)
    maximum_ttl_seconds: int = Field(default=120, ge=1, le=3600, strict=True)
    maximum_observation_age_seconds: int = Field(default=120, ge=1, le=3600, strict=True)
    minimum_submission_remaining_seconds: int = Field(default=45, ge=1, le=3600, strict=True)
    consumer_maximum_age_seconds: int = Field(default=60, ge=1, le=3600, strict=True)
    minimum_confidence_bps: int = Field(default=7500, ge=0, le=10000, strict=True)
    confirmation_blocks: int = Field(default=2, ge=1, le=64, strict=True)
    plan_lifetime_seconds: int = Field(default=300, ge=1, le=3600, strict=True)
    maximum_rpc_lag_seconds: int = Field(default=60, ge=1, le=300, strict=True)
    maximum_initial_signers: int = Field(default=8, ge=1, le=8, strict=True)
    maximum_gas_limit: int = Field(default=8_000_000, ge=21000, le=8_000_000, strict=True)
    maximum_fee_per_gas_wei: POSITIVE = 20_000_000_000
    maximum_priority_fee_per_gas_wei: POSITIVE = 2_000_000_000
    maximum_transaction_cost_wei: POSITIVE = 200_000_000_000_000_000

    @model_validator(mode="after")
    def cadence(self):
        if not self.dataset.startswith("replay:"):
            raise ValueError(
                "the initial public testnet profile requires an isolated replay dataset"
            )
        if self.publication_interval_seconds % 10:
            raise ValueError("publication cadence must align with the 10-second round grid")
        if (
            not self.publication_interval_seconds
            < self.minimum_submission_remaining_seconds
            < min(self.maximum_ttl_seconds, self.maximum_observation_age_seconds)
        ):
            raise ValueError("publication cadence, lifetime, and submission margin do not agree")
        if self.maximum_priority_fee_per_gas_wei > self.maximum_fee_per_gas_wei:
            raise ValueError("priority fee exceeds total fee cap")
        return self

    @property
    def profile_id(self):
        return checksum(self.model_dump(mode="json"))


class UnsignedTransaction(FrozenModel):
    model_config = ConfigDict(populate_by_name=True)
    type: Literal[2] = 2
    chainId: Literal[11155111] = 11155111
    sender: Address = Field(alias="from")
    to: Address | None = Field(default=None, exclude_if=lambda value: value is None)
    nonce: int = Field(ge=0, lt=2**64 - 1, strict=True)
    value: Literal[0] = 0
    gas: int = Field(ge=21000, le=8_000_000, strict=True)
    maxFeePerGas: POSITIVE
    maxPriorityFeePerGas: POSITIVE
    data: HexData

    _sender = field_validator("sender")(address)

    @field_validator("to")
    @classmethod
    def recipient(cls, value):
        return address(value) if value is not None else None


class PlanData(FrozenModel):
    schema_version: Literal[1] = 1
    profile_id: Sha256
    operation: Operation
    artifact_id: Sha256
    transaction: UnsignedTransaction
    arguments: dict
    created_at: Uint64
    expires_at: Uint64
    expected_contract_address: Address | None = None
    oracle: Address | None = None
    administrator: Address | None = None
    registry_id: Sha256 | None = None
    policy_id: Sha256 | None = None
    configuration_hash: Sha256 | None = None
    evidence_root: Sha256 | None = None


class TransactionPlan(PlanData):
    plan_id: Sha256

    @model_validator(mode="after")
    def identity(self):
        data = self.model_dump(mode="json", by_alias=True, exclude={"plan_id"})
        if checksum(data) != self.plan_id:
            raise ValueError("transaction plan checksum mismatch")
        return self


def create_address(sender, nonce):
    return to_checksum_address(
        keccak(rlp.encode([bytes.fromhex(address(sender)[2:]), nonce]))[-20:]
    )


def artifact_for(profile, operation):
    consumer = operation == "deploy-consumer"
    name = "FailClosedConsumer" if consumer else "DiagnosticOracle"
    identity = profile.consumer_artifact_id if consumer else profile.oracle_artifact_id
    return load_artifact(name, identity), identity


def validate_plan(plan, profile, *, expected_plan_id=None, now=None):
    plan, profile = TransactionPlan.model_validate(plan), NetworkProfile.model_validate(profile)
    if now is not None:
        now = TypeAdapter(Uint64).validate_python(now)
    if plan.profile_id != profile.profile_id or (
        expected_plan_id is not None and expected_plan_id != plan.plan_id
    ):
        raise ValueError("plan does not match the independently reviewed plan/profile ID")
    tx = plan.transaction
    if not plan.created_at < plan.expires_at <= plan.created_at + profile.plan_lifetime_seconds:
        raise ValueError("invalid transaction review window")
    if now is not None and not plan.created_at <= now < plan.expires_at:
        raise ValueError("transaction plan is not yet valid or has expired")
    if (
        tx.gas > profile.maximum_gas_limit
        or tx.maxFeePerGas > profile.maximum_fee_per_gas_wei
        or tx.maxPriorityFeePerGas > min(tx.maxFeePerGas, profile.maximum_priority_fee_per_gas_wei)
    ):
        raise ValueError("transaction exceeds gas or fee policy")
    if tx.gas * tx.maxFeePerGas > profile.maximum_transaction_cost_wei:
        raise ValueError("transaction exceeds the approved worst-case cost cap")
    artifact, artifact_id = artifact_for(profile, plan.operation)
    if plan.artifact_id != artifact_id:
        raise ValueError("plan artifact changed")
    name = {
        "deploy-oracle": "constructor",
        "deploy-consumer": "constructor",
        "initialize": "initialize",
        "submit": "submit",
    }[plan.operation]
    if encode_arguments(artifact, name, plan.arguments) != tx.data:
        raise ValueError("transaction calldata differs from reviewed arguments")
    if plan.operation.startswith("deploy-"):
        if tx.to is not None or plan.expected_contract_address != create_address(
            tx.sender, tx.nonce
        ):
            raise ValueError("deployment target/nonce prediction mismatch")
    elif plan.oracle is None or tx.to != plan.oracle or plan.expected_contract_address is not None:
        raise ValueError("oracle call target mismatch")
    if plan.operation == "deploy-oracle":
        if plan.arguments != {
            "initialOwner": plan.administrator,
            "expectedDatasetId": "0x" + keccak(text=profile.dataset).hex(),
        }:
            raise ValueError("oracle constructor context mismatch")
    elif plan.operation == "deploy-consumer":
        if plan.oracle is None or plan.arguments != {
            "source": plan.oracle,
            "age": profile.consumer_maximum_age_seconds,
            "confidenceBps": profile.minimum_confidence_bps,
        }:
            raise ValueError("consumer constructor context mismatch")
    elif plan.operation == "initialize":
        a = plan.arguments
        if (
            plan.administrator != tx.sender
            or a["ttl"] != profile.maximum_ttl_seconds
            or a["age"] != profile.maximum_observation_age_seconds
            or not 1 <= len(a["snapshot"]) <= profile.maximum_initial_signers
        ):
            raise ValueError("initialization role, bounds, or registry size mismatch")
        if (a["expectedPolicyId"], a["expectedConfigurationHash"], a["initialRegistryId"]) != tuple(
            "0x" + (item or "")
            for item in (plan.policy_id, plan.configuration_hash, plan.registry_id)
        ):
            raise ValueError("initialization commitment mismatch")
    else:
        a = plan.arguments["report"]
        if (
            a["feedId"],
            a["datasetId"],
            a["policyId"],
            a["configurationHash"],
            a["evidenceRoot"],
        ) != (
            "0x" + keccak(text="ETH-USD").hex(),
            "0x" + keccak(text=profile.dataset).hex(),
            "0x" + (plan.policy_id or ""),
            "0x" + (plan.configuration_hash or ""),
            "0x" + (plan.evidence_root or ""),
        ):
            raise ValueError("publication context mismatch")
        if (
            a["schemaVersion"],
            a["decimals"],
            a["status"],
            a["value"],
            a["confidenceBps"],
            a["publicationReady"],
        ) != (1, 18, 0, 0, 0, False):
            raise ValueError("testnet v1 permits unavailable diagnostics only")
        if plan.expires_at > a["validUntil"] - profile.minimum_submission_remaining_seconds:
            raise ValueError("publication plan has insufficient expiry margin")
    return plan


def _plan(
    profile,
    operation,
    sender,
    nonce,
    arguments,
    *,
    now,
    gas=None,
    max_fee=None,
    priority_fee=None,
    **context,
):
    profile = NetworkProfile.model_validate(profile)
    artifact, artifact_id = artifact_for(profile, operation)
    sender = address(sender)
    deploy = operation.startswith("deploy-")
    target = None if deploy else context["oracle"]
    method_name = "constructor" if deploy else operation
    data = encode_arguments(artifact, method_name, arguments)
    expiry = now + profile.plan_lifetime_seconds
    if operation == "submit":
        expiry = min(
            expiry, arguments["report"]["validUntil"] - profile.minimum_submission_remaining_seconds
        )
    body = PlanData(
        profile_id=profile.profile_id,
        operation=operation,
        artifact_id=artifact_id,
        transaction=UnsignedTransaction(
            **{
                "from": sender,
                "to": target,
                "nonce": nonce,
                "gas": gas if gas is not None else DEFAULT_GAS[operation],
                "maxFeePerGas": max_fee if max_fee is not None else profile.maximum_fee_per_gas_wei,
                "maxPriorityFeePerGas": priority_fee
                if priority_fee is not None
                else profile.maximum_priority_fee_per_gas_wei,
                "data": data,
            }
        ),
        arguments=arguments,
        created_at=now,
        expires_at=expiry,
        expected_contract_address=create_address(sender, nonce) if deploy else None,
        **context,
    )
    payload = body.model_dump(mode="json", by_alias=True)
    return validate_plan(TransactionPlan(**payload, plan_id=checksum(payload)), profile, now=now)


def deployment_plan(
    profile, *, operation, sender, nonce, now, administrator=None, oracle=None, **fees
):
    profile = NetworkProfile.model_validate(profile)
    if operation == "deploy-oracle":
        administrator = address(administrator)
        arguments = {
            "initialOwner": administrator,
            "expectedDatasetId": "0x" + keccak(text=profile.dataset).hex(),
        }
        return _plan(
            profile,
            operation,
            sender,
            nonce,
            arguments,
            now=now,
            administrator=administrator,
            **fees,
        )
    if operation != "deploy-consumer":
        raise ValueError("unsupported deployment operation")
    oracle = address(oracle)
    arguments = {
        "source": oracle,
        "age": profile.consumer_maximum_age_seconds,
        "confidenceBps": profile.minimum_confidence_bps,
    }
    return _plan(profile, operation, sender, nonce, arguments, now=now, oracle=oracle, **fees)


def _matching_policy(profile, policy, oracle):
    profile, policy = NetworkProfile.model_validate(profile), ReportPolicy.model_validate(policy)
    if (
        policy.domain.chain_id,
        policy.domain.verifying_contract,
        policy.dataset,
        policy.version,
        policy.maximum_ttl_seconds,
        policy.maximum_observation_age_seconds,
    ) != (
        profile.chain_id,
        address(oracle),
        profile.dataset,
        profile.version,
        profile.maximum_ttl_seconds,
        profile.maximum_observation_age_seconds,
    ):
        raise ValueError("signing policy does not match the selected network profile")
    return profile, policy


def initialization_plan(
    profile, *, sender, nonce, oracle, bundle, policy, registry, expected_registry_id, now, **fees
):
    profile, policy = _matching_policy(profile, policy, oracle)
    bundle = verify_bundle(bundle)
    if bundle.dataset != profile.dataset:
        raise ValueError("initialization evidence belongs to another dataset")
    registry = SignerRegistry.model_validate(registry)
    public = registry_inputs(
        policy=policy, registry=registry, expected_registry_id=expected_registry_id
    )
    if registry.revision != 1 or any(key.signer == address(sender) for key in registry.keys):
        raise ValueError(
            "initialization needs a genesis registry and separate administrator/report keys"
        )
    config = configuration_hash(bundle, policy)
    arguments = {
        "expectedPolicyId": public["policy_id"],
        "expectedConfigurationHash": "0x" + config,
        "ttl": profile.maximum_ttl_seconds,
        "age": profile.maximum_observation_age_seconds,
        "initialRegistryId": public["registry_id"],
        "snapshot": public["grants"],
    }
    return _plan(
        profile,
        "initialize",
        sender,
        nonce,
        arguments,
        now=now,
        oracle=address(oracle),
        administrator=address(sender),
        registry_id=registry.registry_id,
        policy_id=policy.policy_id,
        configuration_hash=config,
        evidence_root=bundle.root,
        **fees,
    )


def publication_plan(
    profile,
    *,
    sender,
    nonce,
    oracle,
    signed,
    bundle,
    policy,
    registry,
    expected_registry_id,
    now,
    **fees,
):
    profile, policy = _matching_policy(profile, policy, oracle)
    signed = SignedDiagnosticReport.model_validate(signed)
    registry = SignerRegistry.model_validate(registry)
    bundle = EvidenceBundle.model_validate(bundle)
    value = contract_inputs(
        signed,
        bundle=bundle,
        policy=policy,
        registry=registry,
        expected_registry_id=expected_registry_id,
        now=now,
    )
    return _plan(
        profile,
        "submit",
        sender,
        nonce,
        {"report": value["report"], "signature": value["signature"]},
        now=now,
        oracle=address(oracle),
        registry_id=registry.registry_id,
        policy_id=policy.policy_id,
        configuration_hash=signed.message.configuration_hash,
        evidence_root=bundle.root,
        **fees,
    )


def publication_decision(profile, *, now, cutoff, last_round, in_flight=False):
    """A scheduling decision, not an automatic publisher or freshness override."""
    profile = NetworkProfile.model_validate(profile)
    now, cutoff, last_round = (
        TypeAdapter(Uint64).validate_python(value) for value in (now, cutoff, last_round)
    )
    if type(in_flight) is not bool:
        raise ValueError("pending-transaction state must be a boolean")
    if in_flight:
        return "wait-for-pending-transaction"
    if cutoff <= last_round:
        return "skip-already-published"
    if cutoff > now or cutoff % profile.publication_interval_seconds:
        return "wait-for-round-boundary"
    if (
        cutoff + profile.maximum_observation_age_seconds - now
        <= profile.minimum_submission_remaining_seconds
    ):
        return "skip-expiring-evidence"
    if last_round and cutoff - last_round < profile.publication_interval_seconds:
        return "wait-for-publication-cadence"
    return "ready-for-verified-plan"
