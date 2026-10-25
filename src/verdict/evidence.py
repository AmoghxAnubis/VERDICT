"""Versioned evidence encoding, self-contained round bundles, and inclusion proofs."""

import hashlib
import math
import struct
from decimal import ROUND_HALF_EVEN, localcontext
from typing import Literal

from pydantic import Field, TypeAdapter

from verdict.aggregation import AggregationReport, aggregate
from verdict.confidence import ConfidenceReport, assess_confidence
from verdict.dependencies import DependencyRegistry, FrozenModel, Sha256
from verdict.models import Dataset, Observation
from verdict.scoring import ScoringReport, score_observations
from verdict.weighting import WeightingReport, advance_weights

DOMAIN = b"VERDICT-EVIDENCE-V1\x00"
MAX_LEAVES = 32768
MAX_WEIGHTING_HISTORY = 512
Kind = Literal["assessment", "aggregation", "weighting", "scoring", "registry", "observation"]


def size(value: int) -> bytes:
    return value.to_bytes(4, "big")


def canonical_bytes(value, *, _depth=0) -> bytes:
    """Encode JSON values with explicit types and lengths; no locale or JSON floats."""
    if _depth > 128:
        raise ValueError("evidence nesting exceeds 128 levels")
    if value is None:
        return b"\x00"
    if type(value) is bool:
        return b"\x02" if value else b"\x01"
    if type(value) is int:
        data = str(value).encode("ascii")
        return b"\x03" + size(len(data)) + data
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite evidence number")
        return b"\x04" + struct.pack(">d", value)
    if type(value) is str:
        data = value.encode("utf-8", errors="strict")
        return b"\x05" + size(len(data)) + data
    if type(value) is list:
        return (
            b"\x06"
            + size(len(value))
            + b"".join(canonical_bytes(item, _depth=_depth + 1) for item in value)
        )
    if type(value) is dict and all(type(key) is str for key in value):
        return (
            b"\x07"
            + size(len(value))
            + b"".join(
                canonical_bytes(key, _depth=_depth + 1)
                + canonical_bytes(value[key], _depth=_depth + 1)
                for key in sorted(value, key=lambda item: item.encode("utf-8"))
            )
        )
    raise ValueError("evidence requires JSON values and string object keys")


class EvidenceLeaf(FrozenModel):
    schema_version: Literal[1] = 1
    dataset: Dataset
    assessment_id: Sha256
    kind: Kind
    record_id: Sha256
    payload: dict


class EvidenceBundle(FrozenModel):
    schema_version: Literal[1] = 1
    encoding: Literal["verdict-tlv-v1"] = "verdict-tlv-v1"
    tree: Literal["verdict-merkle-v1"] = "verdict-merkle-v1"
    dataset: Dataset
    assessment_id: Sha256
    root: Sha256
    leaves: tuple[EvidenceLeaf, ...] = Field(min_length=1, max_length=MAX_LEAVES)


class InclusionProof(FrozenModel):
    schema_version: Literal[1] = 1
    tree: Literal["verdict-merkle-v1"] = "verdict-merkle-v1"
    leaf: EvidenceLeaf
    leaf_index: int = Field(ge=0, lt=MAX_LEAVES, strict=True)
    leaf_count: int = Field(ge=1, le=MAX_LEAVES, strict=True)
    siblings: tuple[Sha256, ...] = Field(max_length=15)


def digest(tag: bytes, data: bytes) -> bytes:
    return hashlib.sha256(DOMAIN + tag + data).digest()


def leaf_hash(leaf: EvidenceLeaf) -> bytes:
    return digest(b"\x00", canonical_bytes(leaf.model_dump(mode="json")))


def split(count: int) -> int:
    return 1 << ((count - 1).bit_length() - 1)


def tree_hash(hashes: list[bytes]) -> bytes:
    if len(hashes) == 1:
        return hashes[0]
    left = split(len(hashes))
    return digest(b"\x01", tree_hash(hashes[:left]) + tree_hash(hashes[left:]))


def evidence_root(leaves: tuple[EvidenceLeaf, ...]) -> str:
    if not 1 <= len(leaves) <= MAX_LEAVES:
        raise ValueError("invalid evidence leaf count")
    # Seal the count into the externally trusted root, including for odd trees.
    return digest(b"\x02", size(len(leaves)) + tree_hash([leaf_hash(x) for x in leaves])).hex()


def verify_bundle(bundle: EvidenceBundle) -> EvidenceBundle:
    """Check the root, exact evidence closure, identities, and reproduced pipeline."""
    bundle = EvidenceBundle.model_validate(bundle)
    keys = [(leaf.kind, leaf.record_id) for leaf in bundle.leaves]
    if keys != sorted(set(keys)):
        raise ValueError("evidence leaves must be unique and canonically ordered")
    if any(
        (leaf.dataset, leaf.assessment_id) != (bundle.dataset, bundle.assessment_id)
        for leaf in bundle.leaves
    ):
        raise ValueError("evidence context mismatch")
    if evidence_root(bundle.leaves) != bundle.root:
        raise ValueError("evidence root mismatch")
    records = dict(zip(keys, bundle.leaves, strict=True))
    used = set()

    def record(kind, identity, model):
        key = (kind, identity)
        if key not in records:
            raise ValueError(f"missing {kind} evidence")
        used.add(key)
        result = model.model_validate(records[key].payload)
        actual = (
            result.registry_id
            if kind == "registry"
            else result.observation_id
            if kind == "observation"
            else result.report_id
        )
        if actual != identity or (kind != "registry" and result.dataset != bundle.dataset):
            raise ValueError(f"{kind} evidence identity mismatch")
        if canonical_bytes(result.model_dump(mode="json")) != canonical_bytes(records[key].payload):
            raise ValueError(f"noncanonical {kind} evidence payload")
        return result

    assessment = record("assessment", bundle.assessment_id, ConfidenceReport)
    candidate = record("aggregation", assessment.aggregation_report_id, AggregationReport)
    chain, seen = [], set()
    identity = candidate.weighting_report_id
    while identity is not None:
        if identity in seen or len(chain) >= MAX_WEIGHTING_HISTORY:
            raise ValueError("invalid or oversized weighting evidence history")
        seen.add(identity)
        weights = record("weighting", identity, WeightingReport)
        chain.append(weights)
        identity = weights.previous_report_id
    previous = None
    with localcontext() as context:
        context.prec, context.rounding = 60, ROUND_HALF_EVEN
        for weights in reversed(chain):
            score = record("scoring", weights.scoring_report_id, ScoringReport)
            registry = record("registry", score.registry_id, DependencyRegistry)
            inputs = [
                record("observation", key, Observation) for key in score.input_observation_ids
            ]
            reproduced = score_observations(
                inputs,
                dataset=bundle.dataset,
                cutoff=score.cutoff,
                registry=registry,
                policy=score.policy,
            )
            if reproduced != score:
                raise ValueError("scoring evidence does not reproduce")
            if (
                advance_weights(score, registry, policy=weights.policy, previous=previous)
                != weights
            ):
                raise ValueError("weighting evidence does not reproduce")
            previous = weights
        if aggregate(score, weights) != candidate:
            raise ValueError("aggregation evidence does not reproduce")
        if assess_confidence(candidate, score, registry, policy=assessment.policy) != assessment:
            raise ValueError("assessment evidence does not reproduce")
    if used != set(records):
        raise ValueError("bundle contains unrelated evidence")
    return bundle


def build_bundle(store, *, dataset: str, assessment_id: str) -> EvidenceBundle:
    dataset = TypeAdapter(Dataset).validate_python(dataset)
    assessment_id = TypeAdapter(Sha256).validate_python(assessment_id)
    records = {}

    def retain(kind, identity, payload):
        if payload is None:
            raise ValueError(f"missing retained {kind} evidence")
        records[kind, identity] = EvidenceLeaf(
            dataset=dataset,
            assessment_id=assessment_id,
            kind=kind,
            record_id=identity,
            payload=payload,
        )
        if len(records) > MAX_LEAVES:
            raise ValueError("evidence leaf limit exceeded")
        return payload

    assessment = store.get_confidence_round(assessment_id, dataset=dataset)
    if assessment is None:
        raise LookupError("assessment not found in selected dataset")
    retain("assessment", assessment_id, assessment)
    identity = assessment["aggregation_report_id"]
    candidate = retain(
        "aggregation", identity, store.get_aggregation_round(identity, dataset=dataset)
    )
    identity = candidate["weighting_report_id"]
    count = 0
    while identity:
        if ("weighting", identity) in records or count >= MAX_WEIGHTING_HISTORY:
            raise ValueError("invalid or oversized weighting evidence history")
        count += 1
        weights = retain(
            "weighting", identity, store.get_weighting_round(identity, dataset=dataset)
        )
        key = weights["scoring_report_id"]
        score = retain("scoring", key, store.get_scoring_round(key, dataset=dataset))
        registry = store.get_dependency_registry(score["registry_id"])
        retain(
            "registry", score["registry_id"], registry.model_dump(mode="json") if registry else None
        )
        for row in store.observations_by_ids(score["input_observation_ids"], dataset=dataset):
            retain("observation", row.observation_id, row.model_dump(mode="json"))
        identity = weights["previous_report_id"]
    leaves = tuple(records[key] for key in sorted(records))
    return verify_bundle(
        EvidenceBundle(
            dataset=dataset, assessment_id=assessment_id, leaves=leaves, root=evidence_root(leaves)
        )
    )


def inclusion_proof(bundle: EvidenceBundle, index: int) -> InclusionProof:
    bundle = EvidenceBundle.model_validate(bundle)
    if evidence_root(bundle.leaves) != bundle.root:
        raise ValueError("evidence root mismatch")
    if type(index) is not int or not 0 <= index < len(bundle.leaves):
        raise ValueError("invalid evidence leaf index")

    def path(hashes, position):
        if len(hashes) == 1:
            return []
        left = split(len(hashes))
        if position < left:
            return [*path(hashes[:left], position), tree_hash(hashes[left:]).hex()]
        return [*path(hashes[left:], position - left), tree_hash(hashes[:left]).hex()]

    return InclusionProof(
        leaf=bundle.leaves[index],
        leaf_index=index,
        leaf_count=len(bundle.leaves),
        siblings=tuple(path([leaf_hash(leaf) for leaf in bundle.leaves], index)),
    )


def verify_inclusion(proof: InclusionProof, *, expected_root: str) -> bool:
    """Verify inclusion against a root obtained independently of the proof."""
    try:
        proof = InclusionProof.model_validate(proof)
        expected_root = TypeAdapter(Sha256).validate_python(expected_root)
        if proof.leaf_index >= proof.leaf_count:
            return False
        siblings = iter(proof.siblings)

        def rebuild(index, count):
            if count == 1:
                return leaf_hash(proof.leaf)
            left = split(count)
            if index < left:
                result = rebuild(index, left)
                return digest(b"\x01", result + bytes.fromhex(next(siblings)))
            result = rebuild(index - left, count - left)
            return digest(b"\x01", bytes.fromhex(next(siblings)) + result)

        root = rebuild(proof.leaf_index, proof.leaf_count)
        if next(siblings, None) is not None:
            return False
        return digest(b"\x02", size(proof.leaf_count) + root).hex() == expected_root
    except (ValueError, TypeError, StopIteration, OverflowError):
        return False
