"""Offline EIP-712 diagnostic reports and externally trusted signer registries."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_keys.exceptions import BadSignature
from eth_utils import keccak, to_checksum_address
from pydantic import Field, TypeAdapter, field_validator, model_validator

from verdict.dependencies import FrozenModel, Identifier, Sha256, checksum
from verdict.evidence import EvidenceBundle, verify_bundle
from verdict.models import Address, Asset, Dataset

Uint64 = Annotated[int, Field(ge=0, le=2**64 - 1, strict=True)]
Signature = Annotated[str, Field(pattern=r"^0x[0-9a-f]{130}$")]
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def unix_seconds(value: datetime) -> int:
    if value.tzinfo is None or value.microsecond:
        raise ValueError("report times require timezone-aware whole seconds")
    return TypeAdapter(Uint64).validate_python(
        (value.astimezone(UTC) - EPOCH) // timedelta(seconds=1)
    )


def address(value: str) -> str:
    value = TypeAdapter(Address).validate_python(value)
    if int(value, 16) == 0:
        raise ValueError("zero addresses are not permitted")
    return to_checksum_address(value)


class ReportDomain(FrozenModel):
    name: Literal["VERDICT Diagnostic Reports"] = "VERDICT Diagnostic Reports"
    version: Literal["1"] = "1"
    chain_id: int = Field(ge=1, lt=2**256, strict=True)
    verifying_contract: Address

    _address = field_validator("verifying_contract")(address)

    def typed_domain(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "chainId": self.chain_id,
            "verifyingContract": self.verifying_contract,
        }


class ReportPolicy(FrozenModel):
    schema_version: Literal[1] = 1
    version: Identifier
    domain: ReportDomain
    dataset: Dataset
    asset: Asset = "ETH-USD"
    decimals: Literal[18] = 18
    maximum_ttl_seconds: int = Field(default=30, ge=1, le=3600, strict=True)
    maximum_observation_age_seconds: int = Field(default=30, ge=1, le=3600, strict=True)

    @property
    def policy_id(self) -> str:
        return checksum(self.model_dump(mode="json"))


class SignerAuthorization(FrozenModel):
    key_id: Identifier
    signer: Address
    valid_from: Uint64
    valid_until: Uint64
    revoked_at: Uint64 | None = None

    _address = field_validator("signer")(address)

    @model_validator(mode="after")
    def window(self):
        if self.valid_until <= self.valid_from:
            raise ValueError("signer validity window must be nonempty")
        return self


class SignerRegistry(FrozenModel):
    schema_version: Literal[1] = 1
    domain: ReportDomain
    dataset: Dataset
    policy_id: Sha256
    revision: int = Field(ge=1, strict=True)
    previous_registry_id: Sha256 | None = None
    keys: tuple[SignerAuthorization, ...] = Field(min_length=1, max_length=128)

    @field_validator("keys")
    @classmethod
    def canonical_keys(cls, value):
        return tuple(sorted(value, key=lambda item: item.key_id))

    @model_validator(mode="after")
    def identities(self):
        if (self.revision == 1) != (self.previous_registry_id is None):
            raise ValueError("registry revision and predecessor do not agree")
        if len({key.key_id for key in self.keys}) != len(self.keys) or len(
            {key.signer for key in self.keys}
        ) != len(self.keys):
            raise ValueError("signer IDs and addresses must be unique")
        return self

    @property
    def registry_id(self) -> str:
        return checksum(self.model_dump(mode="json"))


def validate_successor(previous: SignerRegistry, successor: SignerRegistry) -> SignerRegistry:
    previous, successor = (
        SignerRegistry.model_validate(previous),
        SignerRegistry.model_validate(successor),
    )
    if (
        successor.previous_registry_id != previous.registry_id
        or successor.revision != previous.revision + 1
        or (successor.domain, successor.dataset, successor.policy_id)
        != (previous.domain, previous.dataset, previous.policy_id)
    ):
        raise ValueError("signer registry successor context or predecessor mismatch")
    current = {key.key_id: key for key in successor.keys}
    for old in previous.keys:
        new = current.get(old.key_id)
        if new is None or (new.signer, new.valid_from) != (old.signer, old.valid_from):
            raise ValueError("existing signer identities cannot be removed or changed")
        if new.valid_until > old.valid_until or (
            old.revoked_at is not None
            and (new.revoked_at is None or new.revoked_at > old.revoked_at)
        ):
            raise ValueError("retired or revoked authorizations cannot be extended")
    return successor


def rotate_registry(
    previous, *, old_key_id, new_key_id, new_signer, activate_at, retire_at, valid_until
):
    previous = SignerRegistry.model_validate(previous)
    old = next((key for key in previous.keys if key.key_id == old_key_id), None)
    if old is None:
        raise ValueError("old signer ID not found")
    new = SignerAuthorization(
        key_id=new_key_id, signer=new_signer, valid_from=activate_at, valid_until=valid_until
    )
    retire_at = TypeAdapter(Uint64).validate_python(retire_at)
    if (
        not old.valid_from <= activate_at <= retire_at <= old.valid_until
        or valid_until <= retire_at
    ):
        raise ValueError("rotation requires a bounded overlap and a successor beyond retirement")
    keys = tuple(
        key.model_copy(update={"valid_until": retire_at}) if key == old else key
        for key in previous.keys
    )
    return validate_successor(
        previous,
        SignerRegistry(
            **previous.model_dump(exclude={"keys", "revision", "previous_registry_id"}),
            keys=(*keys, new),
            revision=previous.revision + 1,
            previous_registry_id=previous.registry_id,
        ),
    )


def revoke_registry(previous, *, key_id, at):
    previous = SignerRegistry.model_validate(previous)
    at = TypeAdapter(Uint64).validate_python(at)
    if not any(key.key_id == key_id for key in previous.keys):
        raise ValueError("signer ID not found")
    keys = tuple(
        key.model_copy(update={"revoked_at": at}) if key.key_id == key_id else key
        for key in previous.keys
    )
    return validate_successor(
        previous,
        SignerRegistry(
            **previous.model_dump(exclude={"keys", "revision", "previous_registry_id"}),
            keys=keys,
            revision=previous.revision + 1,
            previous_registry_id=previous.registry_id,
        ),
    )


class DiagnosticMessage(FrozenModel):
    schema_version: Literal[1] = 1
    asset: Asset = "ETH-USD"
    dataset: Dataset
    assessment_id: Sha256
    evidence_root: Sha256
    configuration_hash: Sha256
    policy_id: Sha256
    key_id: Identifier
    round_id: Uint64
    observed_at: Uint64
    issued_at: Uint64
    valid_until: Uint64
    decimals: Literal[18] = 18
    status: Literal["unavailable"] = "unavailable"
    value: None = None
    confidence: None = None
    publication_ready: Literal[False] = False

    @model_validator(mode="after")
    def times(self):
        if (
            self.round_id != self.observed_at
            or not self.observed_at <= self.issued_at < self.valid_until
        ):
            raise ValueError("report round or time boundaries are invalid")
        return self


REPORT_FIELDS = (
    ("schemaVersion", "uint16"),
    ("feedId", "bytes32"),
    ("datasetId", "bytes32"),
    ("assessmentId", "bytes32"),
    ("evidenceRoot", "bytes32"),
    ("configurationHash", "bytes32"),
    ("policyId", "bytes32"),
    ("keyId", "bytes32"),
    ("roundId", "uint64"),
    ("observedAt", "uint64"),
    ("issuedAt", "uint64"),
    ("validUntil", "uint64"),
    ("value", "uint256"),
    ("confidenceBps", "uint16"),
    ("status", "uint8"),
    ("decimals", "uint8"),
    ("publicationReady", "bool"),
)


def typed_data(domain: ReportDomain, message: DiagnosticMessage) -> dict:
    domain, message = ReportDomain.model_validate(domain), DiagnosticMessage.model_validate(message)
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "DiagnosticReport": [{"name": name, "type": kind} for name, kind in REPORT_FIELDS],
        },
        "primaryType": "DiagnosticReport",
        "domain": domain.typed_domain(),
        "message": {
            "schemaVersion": message.schema_version,
            "feedId": "0x" + keccak(text=message.asset).hex(),
            "datasetId": "0x" + keccak(text=message.dataset).hex(),
            "assessmentId": "0x" + message.assessment_id,
            "evidenceRoot": "0x" + message.evidence_root,
            "configurationHash": "0x" + message.configuration_hash,
            "policyId": "0x" + message.policy_id,
            "keyId": "0x" + keccak(text=message.key_id).hex(),
            "roundId": message.round_id,
            "observedAt": message.observed_at,
            "issuedAt": message.issued_at,
            "validUntil": message.valid_until,
            # Solidity status 0 means unavailable. Zero payloads are never usable values.
            "value": 0,
            "confidenceBps": 0,
            "status": 0,
            "decimals": message.decimals,
            "publicationReady": False,
        },
    }


def signable(domain, message):
    return encode_typed_data(full_message=typed_data(domain, message))


def report_digest(domain, message) -> str:
    data = signable(domain, message)
    return keccak(b"\x19" + data.version + data.header + data.body).hex()


class SignedDiagnosticReport(FrozenModel):
    schema_version: Literal[1] = 1
    domain: ReportDomain
    message: DiagnosticMessage
    signer: Address
    digest: Sha256
    signature: Signature

    _address = field_validator("signer")(address)


def authorization(registry, policy, key_id):
    registry, policy = SignerRegistry.model_validate(registry), ReportPolicy.model_validate(policy)
    if (registry.domain, registry.dataset, registry.policy_id) != (
        policy.domain,
        policy.dataset,
        policy.policy_id,
    ):
        raise ValueError("signer registry does not authorize this policy or domain")
    grant = next((key for key in registry.keys if key.key_id == key_id), None)
    if grant is None:
        raise ValueError("signer ID is not authorized")
    return grant


def message_from_bundle(bundle, *, policy, registry, key_id, issued_at, valid_until=None):
    bundle, policy = verify_bundle(bundle), ReportPolicy.model_validate(policy)
    if bundle.dataset != policy.dataset:
        raise ValueError("evidence dataset does not match signing policy")
    grant = authorization(registry, policy, key_id)
    issued_at = TypeAdapter(Uint64).validate_python(issued_at)
    records = {(leaf.kind, leaf.record_id): leaf.payload for leaf in bundle.leaves}
    assessment = records["assessment", bundle.assessment_id]
    aggregation = records["aggregation", assessment["aggregation_report_id"]]
    weights = records["weighting", aggregation["weighting_report_id"]]
    score = records["scoring", assessment["scoring_report_id"]]
    observed_at = unix_seconds(datetime.fromisoformat(assessment["cutoff"].replace("Z", "+00:00")))
    if not grant.valid_from <= issued_at < grant.valid_until or (
        grant.revoked_at is not None and issued_at >= grant.revoked_at
    ):
        raise ValueError("signer is inactive or revoked at issuance")
    deadline = min(
        issued_at + policy.maximum_ttl_seconds,
        observed_at + policy.maximum_observation_age_seconds,
        grant.valid_until,
    )
    valid_until = (
        deadline if valid_until is None else TypeAdapter(Uint64).validate_python(valid_until)
    )
    if not observed_at <= issued_at < valid_until <= deadline:
        raise ValueError("evidence is stale or report expiry exceeds policy or signer validity")
    config = checksum(
        {
            "signing_policy_id": policy.policy_id,
            "registry_id": assessment["registry_id"],
            "scoring_policy_sha256": score["policy_sha256"],
            "weighting_policy_sha256": weights["policy_sha256"],
            "confidence_policy_sha256": assessment["policy_sha256"],
            "evidence_encoding": bundle.encoding,
            "evidence_tree": bundle.tree,
            "schemas": {
                kind: payload["schema_version"]
                for kind, payload in (
                    ("assessment", assessment),
                    ("aggregation", aggregation),
                    ("weighting", weights),
                    ("scoring", score),
                )
            },
        }
    )
    return DiagnosticMessage(
        dataset=policy.dataset,
        assessment_id=bundle.assessment_id,
        evidence_root=bundle.root,
        configuration_hash=config,
        policy_id=policy.policy_id,
        key_id=key_id,
        round_id=observed_at,
        observed_at=observed_at,
        issued_at=issued_at,
        valid_until=valid_until,
    )


def sign_report(bundle: EvidenceBundle, *, policy, registry, key_id, account, issued_at):
    policy = ReportPolicy.model_validate(policy)
    message = message_from_bundle(
        bundle, policy=policy, registry=registry, key_id=key_id, issued_at=issued_at
    )
    grant = authorization(registry, policy, key_id)
    if account.address != grant.signer:
        raise ValueError("unlocked key does not match the authorized signer")
    signed = account.sign_message(signable(policy.domain, message))
    return SignedDiagnosticReport(
        domain=policy.domain,
        message=message,
        signer=grant.signer,
        digest=signed.message_hash.hex().removeprefix("0x"),
        signature="0x" + bytes(signed.signature).hex(),
    )


def verify_report(signed, *, bundle, policy, registry, expected_registry_id, now):
    signed, policy, registry = (
        SignedDiagnosticReport.model_validate(signed),
        ReportPolicy.model_validate(policy),
        SignerRegistry.model_validate(registry),
    )
    now = TypeAdapter(Uint64).validate_python(now)
    expected_registry_id = TypeAdapter(Sha256).validate_python(expected_registry_id)
    if registry.registry_id != expected_registry_id:
        raise ValueError("signer registry does not match the independently trusted registry ID")
    if signed.domain != policy.domain:
        raise ValueError("signed report domain mismatch")
    expected = message_from_bundle(
        bundle,
        policy=policy,
        registry=registry,
        key_id=signed.message.key_id,
        issued_at=signed.message.issued_at,
        valid_until=signed.message.valid_until,
    )
    if signed.message != expected or signed.digest != report_digest(signed.domain, signed.message):
        raise ValueError("signed report does not match retained evidence or policy")
    if not signed.message.issued_at <= now < signed.message.valid_until:
        raise ValueError("signed report is not yet valid or has expired")
    grant = authorization(registry, policy, signed.message.key_id)
    if grant.revoked_at is not None and now >= grant.revoked_at:
        raise ValueError("signer is revoked at verification time")
    raw = bytes.fromhex(signed.signature[2:])
    r, s, v = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:64], "big"), raw[64]
    if not (0 < r < SECP256K1_N and 0 < s <= SECP256K1_N // 2 and v in (27, 28)):
        raise ValueError("signature requires canonical low-s secp256k1 and v=27/28")
    try:
        recovered = Account.recover_message(signable(signed.domain, signed.message), signature=raw)
    except BadSignature:
        raise ValueError("invalid secp256k1 signature") from None
    if recovered != grant.signer or signed.signer != grant.signer:
        raise ValueError("signature is not from the authorized signer")
    return signed
