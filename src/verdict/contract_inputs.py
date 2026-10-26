"""Verified ABI-shaped arguments for the diagnostic oracle; never sends a transaction."""

from eth_utils import keccak
from pydantic import TypeAdapter

from verdict.dependencies import Sha256
from verdict.signing import ReportPolicy, SignerRegistry, Uint64, typed_data, verify_report


def registry_inputs(*, policy, registry, expected_registry_id) -> dict:
    policy, registry = ReportPolicy.model_validate(policy), SignerRegistry.model_validate(registry)
    expected_registry_id = TypeAdapter(Sha256).validate_python(expected_registry_id)
    if registry.registry_id != expected_registry_id:
        raise ValueError("registry does not match the independently trusted registry ID")
    if (registry.domain, registry.dataset, registry.policy_id) != (
        policy.domain,
        policy.dataset,
        policy.policy_id,
    ):
        raise ValueError("registry does not authorize this policy or domain")
    revision = TypeAdapter(Uint64).validate_python(registry.revision)
    grants = [
        {
            "keyId": "0x" + keccak(text=key.key_id).hex(),
            "signer": key.signer,
            "validFrom": key.valid_from,
            "validUntil": key.valid_until,
            "revokedAt": key.revoked_at if key.revoked_at is not None else 0,
            "revoked": key.revoked_at is not None,
        }
        for key in registry.keys
    ]
    return {
        "schema_version": 1,
        "chain_id": policy.domain.chain_id,
        "oracle": policy.domain.verifying_contract,
        "registry_id": "0x" + registry.registry_id,
        "registry_revision": revision,
        "previous_registry_id": "0x" + (registry.previous_registry_id or "0" * 64),
        "policy_id": "0x" + policy.policy_id,
        "maximum_ttl": policy.maximum_ttl_seconds,
        "maximum_age": policy.maximum_observation_age_seconds,
        "grants": sorted(grants, key=lambda entry: entry["keyId"]),
    }


def contract_inputs(signed, *, bundle, policy, registry, expected_registry_id, now) -> dict:
    result = registry_inputs(
        policy=policy, registry=registry, expected_registry_id=expected_registry_id
    )
    verified = verify_report(
        signed,
        bundle=bundle,
        policy=policy,
        registry=registry,
        expected_registry_id=expected_registry_id,
        now=now,
    )
    return {
        **result,
        "configuration_hash": "0x" + verified.message.configuration_hash,
        "report": typed_data(verified.domain, verified.message)["message"],
        "signature": verified.signature,
        "digest": "0x" + verified.digest,
        "verified_at": now,
        "publication_ready": False,
    }
