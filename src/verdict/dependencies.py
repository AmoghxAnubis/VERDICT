"""Versioned dependency evidence, not a claim of source independence."""

import hashlib
import json
from datetime import UTC
from importlib.resources import files
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from verdict.models import SOURCES, Asset, Source

Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_:-]{0,95}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Description = Annotated[str, Field(min_length=1, max_length=4000)]


def canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def checksum(value: dict) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class DependencyEvidence(FrozenModel):
    id: Identifier
    kind: Literal["implementation", "primary_document", "review_note"]
    reference: Annotated[str, Field(min_length=1, max_length=500)]
    captured_at: AwareDatetime
    content: Annotated[str, Field(min_length=1, max_length=100000)]
    content_sha256: Sha256

    @field_validator("captured_at")
    @classmethod
    def utc(cls, value):
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def retained_content_matches(self):
        if hashlib.sha256(self.content.encode()).hexdigest() != self.content_sha256:
            raise ValueError("dependency evidence checksum mismatch")
        return self


class DependencyNode(FrozenModel):
    id: Identifier
    kind: Literal["source", "market", "feed", "protocol", "transport", "chain", "asset", "unknown"]
    description: Description
    evidence_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=32)

    @field_validator("evidence_ids")
    @classmethod
    def canonical_evidence(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("duplicate evidence reference")
        return tuple(sorted(value))


class DependencyEdge(FrozenModel):
    downstream: Identifier
    upstream: Identifier
    relationship: Literal["price", "delivery", "settlement", "conversion", "protocol", "unknown"]
    explanation: Description
    evidence_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=32)

    @field_validator("evidence_ids")
    @classmethod
    def canonical_evidence(cls, value):
        return DependencyNode.canonical_evidence(value)


class SourceDependency(FrozenModel):
    source: Source
    node_id: Identifier


class DependencyRegistry(FrozenModel):
    schema_version: Literal[1] = 1
    version: Identifier
    asset: Asset = "ETH-USD"
    reviewed_at: AwareDatetime
    scope: Description
    sources: tuple[SourceDependency, ...] = Field(min_length=6, max_length=6)
    nodes: tuple[DependencyNode, ...] = Field(min_length=6, max_length=256)
    edges: tuple[DependencyEdge, ...] = Field(min_length=1, max_length=1024)
    evidence: tuple[DependencyEvidence, ...] = Field(min_length=1, max_length=64)

    @field_validator("reviewed_at")
    @classmethod
    def utc(cls, value):
        return value.astimezone(UTC)

    @field_validator("sources", "nodes", "edges", "evidence")
    @classmethod
    def canonical_order(cls, value, info):
        if info.field_name == "sources":
            return tuple(sorted(value, key=lambda item: item.source))
        if info.field_name == "edges":
            return tuple(sorted(value, key=lambda item: (item.downstream, item.upstream)))
        return tuple(sorted(value, key=lambda item: item.id))

    @model_validator(mode="after")
    def validate_graph(self):
        nodes = {node.id: node for node in self.nodes}
        evidence = {item.id: item for item in self.evidence}
        if len(nodes) != len(self.nodes) or len(evidence) != len(self.evidence):
            raise ValueError("duplicate node or evidence identity")
        if {source.source for source in self.sources} != set(SOURCES):
            raise ValueError("registry must cover every source exactly once")
        roots = {source.node_id for source in self.sources}
        if len(roots) != len(SOURCES) or roots != {
            node.id for node in self.nodes if node.kind == "source"
        }:
            raise ValueError("each provider requires a distinct source node")
        if any(item.captured_at > self.reviewed_at for item in self.evidence):
            raise ValueError("evidence cannot be captured after registry review")
        used_evidence = set()
        for item in (*self.nodes, *self.edges):
            if not set(item.evidence_ids) <= evidence.keys():
                raise ValueError("unknown evidence reference")
            used_evidence.update(item.evidence_ids)
        if used_evidence != evidence.keys():
            raise ValueError("unreferenced evidence")
        adjacency = {node: set() for node in nodes}
        for edge in self.edges:
            if edge.downstream not in nodes or edge.upstream not in nodes:
                raise ValueError("unknown node reference")
            if edge.upstream in adjacency[edge.downstream]:
                raise ValueError("duplicate dependency edge")
            adjacency[edge.downstream].add(edge.upstream)
            if (nodes[edge.upstream].kind == "unknown") != (edge.relationship == "unknown"):
                raise ValueError("unknown dependencies require an explicit unknown relationship")
            if nodes[edge.downstream].kind == "unknown":
                raise ValueError("unknown dependency must be terminal")
        # Kahn traversal validates cycles without recursion or depending on input order.
        degrees = {node: 0 for node in nodes}
        for upstreams in adjacency.values():
            for upstream in upstreams:
                degrees[upstream] += 1
        pending = [node for node, degree in degrees.items() if degree == 0]
        visited = set()
        while pending:
            node = pending.pop()
            visited.add(node)
            for upstream in adjacency[node]:
                degrees[upstream] -= 1
                if degrees[upstream] == 0:
                    pending.append(upstream)
        if len(visited) != len(nodes):
            raise ValueError("dependency graph must be acyclic")
        reached = set(roots)
        for root in roots:
            reached.update(self.upstreams(root))
            if not adjacency[root]:
                raise ValueError("source must declare dependencies, including unknowns")
        if reached != nodes.keys():
            raise ValueError("unreachable dependency node")
        return self

    def upstreams(self, node_id: str) -> set[str]:
        adjacency: dict[str, set[str]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.downstream, set()).add(edge.upstream)
        pending = list(adjacency.get(node_id, ()))
        result = set()
        while pending:
            current = pending.pop()
            if current not in result:
                result.add(current)
                pending.extend(adjacency.get(current, ()))
        return result

    @property
    def registry_id(self) -> str:
        return checksum(self.model_dump(mode="json"))

    def envelope(self) -> dict:
        return {"registry_id": self.registry_id, **self.model_dump(mode="json")}


def bundled_registry() -> DependencyRegistry:
    return DependencyRegistry.model_validate_json(
        files("verdict").joinpath("data/dependencies-v1.json").read_text(encoding="utf-8")
    )


def inspect_dependencies(
    registry: DependencyRegistry, sources: tuple[Source, ...] = SOURCES
) -> dict:
    """Return overlaps and gaps for an explicit provider set; never count independence."""
    registry = DependencyRegistry.model_validate(registry)
    if not sources or len(set(sources)) != len(sources) or not set(sources) <= set(SOURCES):
        raise ValueError("select one or more unique registered sources")
    roots = {item.source: item.node_id for item in registry.sources}
    nodes = {node.id: node for node in registry.nodes}
    reachable = {source: registry.upstreams(roots[source]) for source in sorted(sources)}
    overlaps = []
    for node_id, node in sorted(nodes.items()):
        affected = [source for source, upstreams in reachable.items() if node_id in upstreams]
        if len(affected) > 1 and node.kind != "unknown":
            overlaps.append({"node_id": node_id, "kind": node.kind, "sources": affected})
    result = {
        "schema_version": 1,
        "algorithm_version": "dependency-inspection-v1",
        "registry_id": registry.registry_id,
        "registry_version": registry.version,
        "sources": [
            {
                "source": source,
                "known_dependencies": sorted(
                    node for node in upstreams if nodes[node].kind != "unknown"
                ),
                "unknown_dependencies": sorted(
                    node for node in upstreams if nodes[node].kind == "unknown"
                ),
            }
            for source, upstreams in reachable.items()
        ],
        "shared_dependencies": overlaps,
        "unresolved_sources": [
            source
            for source, upstreams in reachable.items()
            if any(nodes[node].kind == "unknown" for node in upstreams)
        ],
        "independent_domain_count": None,
        "publication_ready": False,
    }
    return {"inspection_id": checksum(result), **result}
