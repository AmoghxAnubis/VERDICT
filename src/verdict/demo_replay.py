"""Isolated, explicitly synthetic manipulation/outage/recovery evidence."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path

from alembic import command
from alembic.config import Config
from pydantic import TypeAdapter

from verdict.adversarial import synthetic_registry
from verdict.aggregation import aggregate
from verdict.confidence import ConfidencePolicy, assess_confidence
from verdict.dependencies import bundled_registry, checksum
from verdict.evidence import build_bundle, inclusion_proof
from verdict.models import SOURCES, Observation, SourceProvenance
from verdict.scoring import ScoringPolicy, score_observations
from verdict.signing import Uint64
from verdict.signing_keys import write_new_json
from verdict.storage import ObservationStore, build_engine
from verdict.testnet import NetworkProfile
from verdict.weighting import WeightingPolicy

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_START = datetime(2030, 1, 1, tzinfo=UTC)
CHECKPOINTS = (
    (90, "baseline"),
    (150, "manipulation"),
    (270, "attack-recovery"),
    (360, "outage"),
    (510, "recovered"),
)
SCORING = ScoringPolicy(
    window_seconds=60, minimum_fresh_samples=3, minimum_comparisons=3, minimum_unique_observations=3
)
WEIGHTING = WeightingPolicy(
    recovery_new_observations=2,
    recovery_seconds=10,
    increase_per_round_bps=2000,
    source_budget_bps=2000,
)
CONFIDENCE = ConfidencePolicy()


def session_spec(profile, start):
    profile = NetworkProfile.model_validate(profile)
    start = TypeAdapter(Uint64).validate_python(start)
    if start % 30 or profile.publication_interval_seconds != 30:
        raise ValueError("demo requires a 30-second publication grid and aligned session start")
    data = {
        "schema_version": 1,
        "scenario": "testnet-demo-v1",
        "synthetic": True,
        "dataset": profile.dataset,
        "start": start,
        "profile_id": profile.profile_id,
        "manipulation": {
            "start_seconds": 120,
            "end_seconds": 180,
            "source": "coinbase",
            "factor": "1.35",
        },
        "outage": {"start_seconds": 300, "end_seconds": 390, "sources": sorted(SOURCES)},
        "end_seconds": 600,
        "scoring": SCORING.model_dump(mode="json"),
        "weighting": WEIGHTING.model_dump(mode="json"),
        "confidence": CONFIDENCE.model_dump(mode="json"),
        "dependency_registry_id": synthetic_registry(bundled_registry()).registry_id,
    }
    return {**data, "session_id": checksum(data)}


def observations(spec, until):
    if type(until) is not int or not 60 <= until <= spec["end_seconds"] or until % 10:
        raise ValueError(
            "demo cutoff must be on the scoring grid, 60..600 seconds into the session"
        )
    base = datetime.fromtimestamp(spec["start"], UTC)
    rows = []
    for offset in range(0, until + 1, 10):
        if 300 <= offset < 390:
            continue
        for index, source in enumerate(sorted(SOURCES)):
            value = Decimal("99.98") + Decimal(index) / 100
            if source == "coinbase" and 120 <= offset < 180:
                value *= Decimal("1.35")
            kind = {"chainlink": "oracle", "pyth": "oracle", "uniswap_v3": "dex"}.get(
                source, "exchange"
            )
            provenance = (
                SourceProvenance(
                    protocol=source,
                    instrument="synthetic:testnet-demo-v1",
                    verification="synthetic_fixture",
                )
                if kind != "exchange"
                else None
            )
            rows.append(
                Observation(
                    dataset=spec["dataset"],
                    source=source,
                    source_type=kind,
                    provider_event_id=f"demo-{spec['start']}-{source}-{offset}",
                    value=value,
                    timestamp=base + timedelta(seconds=offset),
                    received_at=base + timedelta(seconds=offset),
                    latency_ms=0,
                    provenance=provenance,
                    raw_payload={
                        "synthetic": True,
                        "scenario": spec["scenario"],
                        "session_id": spec["session_id"],
                        "seconds": offset,
                        "value": str(value),
                    },
                )
            )
    return rows


def summarize(bundle):
    records = {(leaf.kind, leaf.record_id): leaf.payload for leaf in bundle.leaves}
    assessment = records["assessment", bundle.assessment_id]
    candidate = records["aggregation", assessment["aggregation_report_id"]]
    weights = records["weighting", candidate["weighting_report_id"]]
    score = records["scoring", weights["scoring_report_id"]]
    samples = {row["source"]: row for row in score["ticks"][-1]["sources"]}
    return {
        "cutoff": assessment["cutoff"],
        "assessment_id": bundle.assessment_id,
        "evidence_root": bundle.root,
        "candidate_value": assessment["candidate_value"],
        "total_weight_bps": weights["total_weight_bps"],
        "status": assessment["status"],
        "value": assessment["value"],
        "confidence": assessment["confidence"],
        "publication_ready": assessment["publication_ready"],
        "sources": {
            row["source"]: {
                "state": row["state"],
                "weight_bps": row["weight_bps"],
                "availability": samples[row["source"]]["availability"],
                "comparison": samples[row["source"]]["comparison"],
            }
            for row in weights["sources"]
        },
    }


def retain_replay(directory, profile, *, start, until, checkpoints):
    """Create a dedicated migrated SQLite database; never read/write the configured app DB."""
    spec = session_spec(profile, start)
    requested = tuple(checkpoints)
    if (
        not requested
        or len(set(requested)) != len(requested)
        or any(type(at) is not int or at % 30 or not 60 <= at <= until for at in requested)
    ):
        raise ValueError("demo checkpoints must be unique publication rounds inside the session")
    with localcontext() as context:
        context.prec, context.rounding = 60, ROUND_HALF_EVEN
        rows = observations(spec, until)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        engine = build_engine(f"sqlite:///{(directory / 'replay.db').resolve().as_posix()}")
        try:
            config = Config(str(ROOT / "alembic.ini"))
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            store = ObservationStore(engine)
            registry = synthetic_registry(bundled_registry())
            store.save_dependency_registry(registry)
            store.add_many(rows)
            timeline, bundles = [], {}
            for offset in range(60, until + 1, 10):
                score = score_observations(
                    rows,
                    dataset=profile.dataset,
                    cutoff=datetime.fromtimestamp(start + offset, UTC),
                    registry=registry,
                    policy=SCORING,
                )
                store.save_scoring_round(score)
                weights = store.apply_weighting(
                    score.report_id, dataset=profile.dataset, policy=WEIGHTING
                )
                candidate = aggregate(score, weights)
                store.save_aggregation_round(candidate)
                quality = assess_confidence(candidate, score, registry, policy=CONFIDENCE)
                store.save_confidence_round(quality)
                timeline.append(
                    {
                        "seconds": offset,
                        "assessment_id": quality.report_id,
                        "candidate_value": str(candidate.candidate_value)
                        if candidate.candidate_value is not None
                        else None,
                        "total_weight_bps": weights.total_weight_bps,
                        "status": quality.status,
                    }
                )
                if offset in requested:
                    bundles[offset] = build_bundle(
                        store, dataset=profile.dataset, assessment_id=quality.report_id
                    )
        finally:
            engine.dispose()
    write_new_json(directory / "session.json", spec)
    write_new_json(directory / "timeline.json", {"rounds": timeline})
    return bundles


def export_evidence(directory, profile, *, start, now):
    """Fresh public-session snapshot. No signing, network calls, or clock override in CLI."""
    now = TypeAdapter(Uint64).validate_python(now)
    cutoff = now - now % 30
    bundle = retain_replay(
        directory, profile, start=start, until=cutoff - start, checkpoints=[cutoff - start]
    )[cutoff - start]
    write_new_json(Path(directory) / "evidence.json", bundle.model_dump(mode="json"))
    index = next(i for i, leaf in enumerate(bundle.leaves) if leaf.kind == "assessment")
    write_new_json(
        Path(directory) / "assessment-proof.json",
        inclusion_proof(bundle, index).model_dump(mode="json"),
    )
    return {
        "synthetic": True,
        "dataset": profile.dataset,
        "cutoff": cutoff,
        "assessment_id": bundle.assessment_id,
        "evidence_root": bundle.root,
        "publication_ready": False,
        "public_deployment": False,
    }
