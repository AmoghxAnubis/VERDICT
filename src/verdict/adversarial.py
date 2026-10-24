"""Reproducible synthetic benchmarks through the complete diagnostic pipeline."""

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from verdict.aggregation import aggregate
from verdict.confidence import ConfidencePolicy, assess_confidence, ceiling
from verdict.dependencies import DependencyRegistry, FrozenModel, bundled_registry, checksum
from verdict.models import SOURCES
from verdict.replay import load_observations
from verdict.scoring import ScoringPolicy, score_observations
from verdict.weighting import WeightingPolicy, advance_weights

BASE = datetime(2026, 10, 20, tzinfo=UTC)
CASES = {
    "baseline": {"sources": []},
    "single_spike": {"sources": ["coinbase"], "factor": "1.35"},
    "market_move": {"sources": sorted(SOURCES), "factor": "1.10"},
    "correlated_pair": {"sources": ["chainlink", "uniswap_v3"], "factor": "1.35"},
    "correlated_majority": {
        "sources": ["coinbase", "kraken", "binance_us", "pyth"],
        "factor": "1.35",
    },
    "source_outage": {"sources": ["coinbase"], "drop": True},
    "shared_outage": {"sources": ["chainlink", "uniswap_v3"], "drop": True},
    "late_receipt": {"sources": ["kraken"], "delay_seconds": 45},
}


class AdversarialSuite(FrozenModel):
    version: Literal["adversarial-v1"] = "adversarial-v1"
    fixture: str = Field(min_length=1, max_length=500)
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scoring: ScoringPolicy
    weighting: WeightingPolicy
    confidence: ConfidencePolicy = ConfidencePolicy()
    first_round_seconds: int = Field(default=60, ge=10, le=3600, strict=True)
    attack_start_seconds: int = Field(default=120, ge=10, le=3600, strict=True)
    attack_end_seconds: int = Field(default=180, ge=10, le=7200, strict=True)
    last_round_seconds: int = Field(default=300, ge=10, le=10800, strict=True)
    maximum_candidate_shift_bps: int = Field(default=5, ge=0, le=10000, strict=True)

    @model_validator(mode="after")
    def grid(self):
        times = (
            self.first_round_seconds,
            self.attack_start_seconds,
            self.attack_end_seconds,
            self.last_round_seconds,
        )
        if not all(a < b for a, b in zip(times, times[1:], strict=False)):
            raise ValueError("benchmark times must be strictly increasing")
        if any(time % self.scoring.step_seconds for time in times):
            raise ValueError("benchmark times must align to scoring steps")
        if self.first_round_seconds < self.scoring.window_seconds:
            raise ValueError("first round requires a complete warmup window")
        if self.last_round_seconds // self.scoring.step_seconds > 360:
            raise ValueError("benchmark exceeds 360 observation steps")
        return self


def synthetic_registry(registry: DependencyRegistry) -> DependencyRegistry:
    """An explicit test assumption, never an independence attestation."""
    data = registry.model_dump(mode="json")
    removed = {node["id"] for node in data["nodes"] if node["kind"] == "unknown"}
    data["nodes"] = [node for node in data["nodes"] if node["id"] not in removed]
    data["edges"] = [edge for edge in data["edges"] if edge["upstream"] not in removed]
    data["version"] = "synthetic-adversarial-graph"
    data["scope"] = (
        "SYNTHETIC TEST ONLY: unknowns removed to exercise nonzero weights; "
        "no independence evidence."
    )
    return DependencyRegistry.model_validate(data)


def observations(templates, suite, name, spec, profile):
    rows = []
    prices = dict(
        zip(
            sorted(SOURCES),
            map(Decimal, ("99.98", "99.99", "100", "100.01", "100.02", "100.03")),
            strict=True,
        )
    )
    for time in range(0, suite.last_round_seconds + 1, suite.scoring.step_seconds):
        for template in templates:
            active = (
                suite.attack_start_seconds <= time < suite.attack_end_seconds
                and template.source in spec["sources"]
            )
            if active and spec.get("drop"):
                continue
            value = prices[template.source]
            if active:
                value *= Decimal(spec.get("factor", "1"))
            timestamp = BASE + timedelta(seconds=time)
            delay = spec.get("delay_seconds", 0) if active else 0
            data = template.model_dump()
            data.update(
                dataset=f"replay:adv-{profile}-{name}",
                provider_event_id=f"synthetic-{template.source}-{time}",
                value=value,
                timestamp=timestamp,
                received_at=timestamp + timedelta(seconds=delay),
                raw_payload={
                    "synthetic": "adversarial-v1",
                    "scenario": name,
                    "source": template.source,
                    "seconds": time,
                    "value": str(value),
                    "delay_seconds": delay,
                },
            )
            # Oracle/DEX provenance is a synthetic fixture template, not a live read.
            rows.append(type(template).model_validate(data))
    return rows


def run_case(rows, suite, registry):
    rounds, previous = [], None
    for time in range(
        suite.first_round_seconds, suite.last_round_seconds + 1, suite.scoring.step_seconds
    ):
        score = score_observations(
            rows,
            dataset=rows[0].dataset,
            cutoff=BASE + timedelta(seconds=time),
            registry=registry,
            policy=suite.scoring,
        )
        weight = advance_weights(score, registry, policy=suite.weighting, previous=previous)
        candidate = aggregate(score, weight)
        quality = assess_confidence(candidate, score, registry, policy=suite.confidence)
        previous = weight
        samples = {item.source: item for item in score.ticks[-1].sources}
        rounds.append(
            {
                "seconds": time,
                "cutoff": score.cutoff.isoformat(),
                "scoring_report_id": score.report_id,
                "weighting_report_id": weight.report_id,
                "aggregation_report_id": candidate.report_id,
                "assessment_id": quality.report_id,
                "input_sha256": score.input_sha256,
                "candidate_value": str(candidate.candidate_value)
                if candidate.candidate_value is not None
                else None,
                "total_weight_bps": weight.total_weight_bps,
                "quality_score_bps": quality.quality_score_bps,
                "gates": [gate.model_dump(mode="json") for gate in quality.gates],
                "status": quality.status,
                "value": quality.value,
                "confidence": quality.confidence,
                "publication_ready": quality.publication_ready,
                "sources": {
                    item.source: {
                        "state": item.state,
                        "weight_bps": item.weight_bps,
                        "reasons": list(item.reasons),
                        "availability": samples[item.source].availability,
                        "comparison": samples[item.source].comparison,
                        "observation_id": samples[item.source].observation_id,
                    }
                    for item in weight.sources
                },
            }
        )
    return rounds


def paired_metrics(clean, changed, suite, spec):
    active = [
        (a, b)
        for a, b in zip(clean, changed, strict=True)
        if suite.attack_start_seconds <= a["seconds"] < suite.attack_end_seconds
    ]
    shifts = [
        ceiling(abs(Fraction(b["candidate_value"]) / Fraction(a["candidate_value"]) - 1) * 10000)
        for a, b in active
        if a["candidate_value"] is not None and b["candidate_value"] is not None
    ]
    delays, recovery = {}, {}
    for source in spec["sources"]:
        failed = [
            b["seconds"] - suite.attack_start_seconds
            for _, b in active
            if b["sources"][source]["state"] == "quarantined"
        ]
        delays[source] = min(failed) if failed else None
        recovered = [
            row["seconds"] - suite.attack_end_seconds
            for row in changed
            if row["seconds"] >= suite.attack_end_seconds
            and row["sources"][source]["weight_bps"] > 0
        ]
        recovery[source] = min(recovered) if recovered and failed else None
    return {
        "active_rounds": len(active),
        "comparable_candidate_rounds": len(shifts),
        "maximum_candidate_shift_bps": max(shifts) if shifts else None,
        "new_missing_candidate_rounds": sum(
            a["candidate_value"] is not None and b["candidate_value"] is None for a, b in active
        ),
        "additional_quarantine_decisions": sum(
            a["sources"][s]["state"] != "quarantined" and b["sources"][s]["state"] == "quarantined"
            for a, b in active
            for s in SOURCES
        ),
        "first_quarantine_delay_seconds": delays,
        "first_positive_weight_after_intervention_seconds": recovery,
    }


def evaluate_adversarial(path: Path) -> dict:
    # Normalizing oracle fixtures also uses Decimal; protect the complete run.
    with localcontext() as context:
        context.prec = 60
        context.rounding = ROUND_HALF_EVEN
        return _evaluate_adversarial(path)


def _evaluate_adversarial(path: Path) -> dict:
    suite = AdversarialSuite.model_validate_json(path.read_text(encoding="utf-8"))
    fixture = path.parent / suite.fixture
    if (
        hashlib.sha256(fixture.read_text(encoding="utf-8").encode()).hexdigest()
        != suite.fixture_sha256
    ):
        raise ValueError("adversarial fixture checksum mismatch")
    templates = sorted(load_observations(fixture), key=lambda row: row.source)
    if len(templates) != 6 or {row.source for row in templates} != set(SOURCES):
        raise ValueError("benchmark fixture requires exactly one observation per provider")
    registry = bundled_registry()
    profiles = {"bundled": registry, "synthetic": synthetic_registry(registry)}
    results = {}
    with localcontext() as context:
        context.prec = 60
        for profile, graph in profiles.items():
            cases = {}
            for name, spec in CASES.items():
                rows = observations(templates, suite, name, spec, profile)
                rounds = run_case(rows, suite, graph)
                cases[name] = {
                    "observation_count": len(rows),
                    "observations_sha256": checksum(
                        {"rows": [r.model_dump(mode="json") for r in rows]}
                    ),
                    "rounds": rounds,
                    "paired": paired_metrics(
                        cases["baseline"]["rounds"] if cases else rounds, rounds, suite, spec
                    ),
                }
            results[profile] = {
                "registry": graph.model_dump(mode="json"),
                "registry_id": graph.registry_id,
                "cases": cases,
            }
    synthetic = results["synthetic"]["cases"]
    checks = {}
    for name in ("single_spike", "correlated_pair", "correlated_majority"):
        measured = synthetic[name]["paired"]["maximum_candidate_shift_bps"]
        checks[name] = {
            "maximum_candidate_shift_bps": measured,
            "required_maximum_bps": suite.maximum_candidate_shift_bps,
            "status": "not_scored"
            if measured is None
            else "pass"
            if measured <= suite.maximum_candidate_shift_bps
            else "fail",
        }
    modules = (
        "adversarial",
        "models",
        "replay",
        "dependencies",
        "scoring",
        "weighting",
        "aggregation",
        "confidence",
    )
    report = {
        "schema_version": 1,
        "suite": suite.model_dump(mode="json"),
        "suite_sha256": checksum(suite.model_dump(mode="json")),
        "interventions": CASES,
        "source_sha256": {
            name: hashlib.sha256(
                Path(__file__).with_name(f"{name}.py").read_text(encoding="utf-8").encode()
            ).hexdigest()
            for name in modules
        },
        "profiles": results,
        "attack_targets": checks,
        "attack_targets_met": all(check["status"] == "pass" for check in checks.values()),
        "publication_ready": False,
        "historical_validation_complete": False,
        "limitations": [
            "Synthetic six-provider diagnostic benchmark, not held-out market evidence.",
            "Shortened scoring/recovery policies are explicit test settings, "
            "not production calibration.",
            "Oracle/DEX provenance is copied from synthetic templates; "
            "no authentic reads or independence are established.",
            "Synthetic registry removes unknowns solely to exercise nonzero allocation.",
            "All availability assessments must remain unavailable because "
            "independent domains are unknown.",
            "Correlated-majority errors measure agreement limits, not manipulation resistance.",
            "Offline pure pipeline; SQL persistence, service load and live availability "
            "are separate validation.",
        ],
    }
    report["report_id"] = checksum(report)
    return report
