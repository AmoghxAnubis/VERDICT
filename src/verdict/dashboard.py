"""Read-only round-linked snapshots and explicitly isolated demo seeding."""

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, localcontext
from pathlib import Path

from verdict.models import SOURCES


def snapshot(store, *, dataset: str, assessment_id: str | None = None) -> dict:
    # A later commit may enter the snapshot but will still be delivered on resume.
    cursor = store.event_cursor()
    history = store.confidence_rounds(dataset=dataset, limit=100)
    report = (
        store.get_confidence_round(assessment_id, dataset=dataset)
        if assessment_id
        else (history[0] if history else None)
    )
    if assessment_id and report is None:
        raise LookupError("assessment not found in selected dataset")
    score = weights = aggregation = graph = None
    if report:
        aggregation = store.get_aggregation_round(report["aggregation_report_id"], dataset=dataset)
        score = store.get_scoring_round(report["scoring_report_id"], dataset=dataset)
        if aggregation is None or score is None:
            raise ValueError("round evidence is missing")
        weights = store.get_weighting_round(aggregation["weighting_report_id"], dataset=dataset)
        registry = store.get_dependency_registry(report["registry_id"])
        if weights is None or registry is None:
            raise ValueError("round evidence is missing")
        if (
            aggregation["scoring_report_id"] != score["report_id"]
            or weights["scoring_report_id"] != score["report_id"]
            or score["registry_id"] != registry.registry_id
        ):
            raise ValueError("round evidence links do not agree")
        graph = registry.envelope()
        graph["evidence"] = [
            {k: v for k, v in item.items() if k != "content"} for item in graph["evidence"]
        ]
    current = {item["source"]: item for item in score["ticks"][-1]["sources"]} if score else {}
    states = {item["source"]: item for item in weights["sources"]} if weights else {}
    quality = {item["source"]: item for item in score["sources"]} if score else {}
    sources = []
    now = datetime.now(UTC)
    for source in sorted(SOURCES):
        if score:
            sources.append(
                {
                    **current[source],
                    **states[source],
                    "score_bps": quality[source]["score_bps"],
                    "score_reasons": quality[source]["reasons"],
                    "unknown_dependencies": quality[source]["unknown_dependencies"],
                }
            )
        else:
            rows = store.list(dataset=dataset, source=source, limit=1)
            row = rows[0] if rows else None
            sources.append(
                {
                    "source": source,
                    "observation_id": row.observation_id if row else None,
                    "value": str(row.value) if row else None,
                    "age_seconds": str((now - row.timestamp).total_seconds()) if row else None,
                    "availability": "unassessed" if row else "missing",
                    "state": "unassessed",
                    "weight_bps": 0,
                    "score_bps": None,
                    "reasons": ["no_retained_assessment"],
                    "unknown_dependencies": [],
                }
            )
    return {
        "asset": "ETH-USD",
        "dataset": dataset,
        "cursor": cursor,
        "mode": "live" if dataset == "live" else "replay",
        "as_of": report["cutoff"] if report else now.isoformat(),
        "assessment": report,
        "aggregation": aggregation,
        "weighting": weights,
        "scoring_report_id": score["report_id"] if score else None,
        "sources": sources,
        "registry": graph,
        "history": [
            {
                k: row[k]
                for k in (
                    "report_id",
                    "cutoff",
                    "candidate_value",
                    "quality_score_bps",
                    "eligible_source_count",
                    "total_weight_bps",
                    "status",
                    "reasons",
                )
            }
            for row in history
        ],
        "publication_ready": False,
        "value": None,
        "confidence": None,
    }


def seed_demo(store, suite_path: Path, *, case: str) -> dict:
    from verdict.adversarial import CASES, AdversarialSuite, observations, synthetic_registry
    from verdict.aggregation import aggregate
    from verdict.confidence import assess_confidence
    from verdict.dependencies import bundled_registry
    from verdict.replay import load_observations
    from verdict.scoring import score_observations

    if case != "all" and case not in CASES:
        raise ValueError("unknown demo case")
    suite = AdversarialSuite.model_validate_json(suite_path.read_text(encoding="utf-8"))
    fixture = suite_path.parent / suite.fixture
    if (
        hashlib.sha256(fixture.read_text(encoding="utf-8").encode()).hexdigest()
        != suite.fixture_sha256
    ):
        raise ValueError("demo fixture checksum mismatch")
    datasets = []
    with localcontext() as context:
        context.prec, context.rounding = 60, ROUND_HALF_EVEN
        templates = load_observations(fixture)
        registry = synthetic_registry(bundled_registry())
        store.save_dependency_registry(registry)
        for name in CASES if case == "all" else [case]:
            rows = observations(templates, suite, name, CASES[name], "synthetic")
            dataset = rows[0].dataset
            if not dataset.startswith("replay:"):
                raise ValueError("demo cannot write live data")
            store.add_many(rows)
            for time in range(
                suite.first_round_seconds, suite.last_round_seconds + 1, suite.scoring.step_seconds
            ):
                score = score_observations(
                    rows,
                    dataset=dataset,
                    cutoff=rows[0].timestamp + timedelta(seconds=time),
                    registry=registry,
                    policy=suite.scoring,
                )
                store.save_scoring_round(score)
                weights = store.apply_weighting(
                    score.report_id, dataset=dataset, policy=suite.weighting
                )
                candidate = aggregate(score, weights)
                store.save_aggregation_round(candidate)
                store.save_confidence_round(
                    assess_confidence(candidate, score, registry, policy=suite.confidence)
                )
            datasets.append(dataset)
    return {"datasets": datasets, "synthetic": True, "publication_ready": False}
