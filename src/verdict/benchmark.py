"""Offline paired diagnostics on frozen historical trades and explicit interventions."""

import gzip
import hashlib
import json
from collections import Counter
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from verdict.historical import capture_path, load_capture, load_suite
from verdict.models import EXCHANGE_SOURCES, Observation
from verdict.statistics import AnalysisReport, analyze, digest

INTERVENTIONS = (
    {"name": "single_spike", "sources": ["coinbase"], "factor": "1.35", "start": 30, "end": 90},
    {
        "name": "coordinated_move",
        "sources": list(EXCHANGE_SOURCES),
        "factor": "1.10",
        "start": 30,
        "end": 90,
    },
    {
        "name": "correlated_shift",
        "sources": ["coinbase", "kraken"],
        "factor": "1.35",
        "start": 30,
        "end": 90,
    },
    {"name": "source_outage", "sources": ["coinbase"], "drop": True, "start": 30, "end": 120},
    {"name": "late_receipt", "sources": ["kraken"], "delay_seconds": 45, "start": 30, "end": 90},
)


def fraction(numerator: int, denominator: int) -> str | None:
    return str(Decimal(numerator) / denominator) if denominator else None


def intervene(rows: list[Observation], case, spec: dict | None) -> list[Observation]:
    name = spec["name"] if spec else "baseline"
    dataset = f"replay:hist-{case.name}-{name}"
    result = []
    for row in rows:
        changes = {"dataset": dataset}
        if (
            spec
            and row.source in spec["sources"]
            and (
                case.start + timedelta(seconds=spec["start"])
                <= row.timestamp
                < case.start + timedelta(seconds=spec["end"])
            )
        ):
            if spec.get("drop"):
                continue
            if "factor" in spec:
                value = row.value * Decimal(spec["factor"])
                if value.as_tuple().exponent < -18:
                    value = value.quantize(Decimal("0.000000000000000001"))
                changes["value"] = value
            if "delay_seconds" in spec:
                changes["received_at"] = row.received_at + timedelta(seconds=spec["delay_seconds"])
            changes["raw_payload"] = {
                "historical_provider_payload": row.raw_payload,
                "benchmark_intervention": spec,
            }
        # Synthetic changes stay in a separate replay dataset, never the retained capture.
        result.append(
            Observation.model_validate_json(row.model_copy(update=changes).model_dump_json())
        )
    return result


def historical_rounds(rows: list[Observation], suite, case, scenario: str) -> list[AnalysisReport]:
    reports = []
    cutoff = case.start
    while cutoff < case.end:
        visible = [row for row in rows if row.timestamp <= cutoff and row.received_at <= cutoff]
        start = cutoff - timedelta(
            seconds=suite.policy.history_seconds + suite.policy.freshness_seconds
        )
        latest = {}
        for row in sorted(
            visible, key=lambda row: (row.timestamp, row.event_sequence, row.observation_id)
        ):
            latest[row.source] = row.observation_id
        snapshot = [
            row
            for row in visible
            if row.timestamp >= start or row.observation_id == latest[row.source]
        ]
        reports.append(
            analyze(
                snapshot,
                dataset=f"replay:hist-{case.name}-{scenario}",
                cutoff=cutoff,
                policy=suite.policy,
            )
        )
        cutoff += timedelta(seconds=suite.step_seconds)
    return reports


def summarize(reports: list[AnalysisReport]) -> dict:
    sources = {}
    for source in reports[0].policy.sources:
        decisions = [
            next(item for item in report.sources if item.source == source) for report in reports
        ]
        exclusions = Counter(reason for item in decisions for reason in item.exclusion_reasons)
        opportunities = sum(
            report.median_reference is not None
            and not any(
                reason in item.exclusion_reasons for reason in ("missing", "stale", "clock_skew")
            )
            for report, item in zip(reports, decisions, strict=True)
        )
        sources[source] = {
            "inlier_rounds": sum(not item.exclusion_reasons for item in decisions),
            "exclusions": dict(sorted(exclusions.items())),
            "comparison_opportunities": opportunities,
            "extreme_deviations": exclusions["extreme_deviation"],
            "high_velocity_signals": sum(
                "high_price_velocity" in item.signals for item in decisions
            ),
        }
    comparisons = sum(item["comparison_opportunities"] for item in sources.values())
    extremes = sum(item["extreme_deviations"] for item in sources.values())
    return {
        "rounds": len(reports),
        "analyzed_rounds": sum(report.status == "analyzed" for report in reports),
        "availability_fraction": fraction(
            sum(report.status == "analyzed" for report in reports), len(reports)
        ),
        "comparison_opportunities": comparisons,
        "extreme_deviations": extremes,
        "unlabelled_exclusion_fraction": fraction(extremes, comparisons),
        "sources": sources,
    }


def round_summary(report: AnalysisReport) -> dict:
    return {
        "cutoff": report.cutoff.isoformat(),
        "round_id": report.round_id,
        "status": report.status,
        "median_reference": str(report.median_reference) if report.median_reference else None,
        "fresh_sources": report.fresh_sources,
        "inlier_sources": report.inlier_sources,
        "sources": {
            item.source: {
                "observation_id": item.observation_id,
                "exclusions": item.exclusion_reasons,
                "signals": item.signals,
            }
            for item in report.sources
        },
    }


def paired_metrics(
    clean: list[AnalysisReport], changed: list[AnalysisReport], case, spec: dict
) -> dict:
    paired = list(zip(clean, changed, strict=True))
    active = [
        (baseline, experiment)
        for baseline, experiment in paired
        if case.start + timedelta(seconds=spec["start"])
        <= baseline.cutoff
        < case.start + timedelta(seconds=spec["end"])
    ]
    shifts = [
        abs(experiment.median_reference / baseline.median_reference - 1) * 10000
        for baseline, experiment in active
        if baseline.median_reference is not None and experiment.median_reference is not None
    ]
    extra = 0
    for baseline, experiment in active:
        for original, modified in zip(baseline.sources, experiment.sources, strict=True):
            extra += (
                not original.exclusion_reasons and "extreme_deviation" in modified.exclusion_reasons
            )
    result = {
        "active_rounds": len(active),
        "additional_extreme_deviations": extra,
        "new_unavailable_rounds": sum(
            baseline.status == "analyzed" and experiment.status == "insufficient_data"
            for baseline, experiment in active
        ),
        "maximum_reference_shift_bps": str(max(shifts)) if shifts else None,
    }
    if spec["name"] == "single_spike":
        opportunities, detections, first = 0, 0, None
        for baseline, experiment in active:
            original = next(item for item in baseline.sources if item.source == "coinbase")
            modified = next(item for item in experiment.sources if item.source == "coinbase")
            if (
                original.exclusion_reasons
                or modified.value is None
                or original.timestamp < case.start + timedelta(seconds=spec["start"])
            ):
                continue
            opportunities += 1
            if "extreme_deviation" in modified.exclusion_reasons:
                detections += 1
                if first is None:
                    first = (baseline.cutoff - case.start).total_seconds() - spec["start"]
        result.update(
            {
                "detection_opportunities": opportunities,
                "detections": detections,
                "detection_fraction": fraction(detections, opportunities),
                "first_detection_delay_seconds": first,
            }
        )
    return result


def target(value: str | None, threshold: Decimal, *, minimum: bool) -> dict:
    passed = (
        None
        if value is None
        else (Decimal(value) >= threshold if minimum else Decimal(value) <= threshold)
    )
    return {
        "value": value,
        "threshold": str(threshold),
        "status": "not_scored" if passed is None else "pass" if passed else "fail",
    }


def evaluate_history(suite_path: Path, directory: Path) -> dict:
    suite = load_suite(suite_path)
    with localcontext() as context:
        context.prec = 60
        cases, captures = [], {}
        for case in suite.cases:
            rows, counts = [], {}
            for source in EXCHANGE_SOURCES:
                path = capture_path(directory, case, source)
                encoded = path.read_bytes()
                if len(encoded) > 50_000_000:
                    raise ValueError("historical archive size limit exceeded")
                raw = gzip.decompress(encoded)
                if len(raw) > 200_000_000:
                    raise ValueError("historical expanded capture size limit exceeded")
                normalized = load_capture(json.loads(raw), suite, case, source)
                counts[source] = len(normalized)
                rows.extend(normalized)
                captures[path.name] = hashlib.sha256(encoded).hexdigest()
            baseline = historical_rounds(intervene(rows, case, None), suite, case, "baseline")
            metrics = summarize(baseline)
            experiments = {}
            for spec in INTERVENTIONS:
                changed = historical_rounds(intervene(rows, case, spec), suite, case, spec["name"])
                experiments[spec["name"]] = {
                    "metrics": summarize(changed),
                    "paired": paired_metrics(baseline, changed, case, spec),
                    "rounds": [round_summary(report) for report in changed],
                }
            comparisons = experiments["single_spike"]["paired"]
            gates = {
                "baseline_availability": target(
                    metrics["availability_fraction"],
                    suite.targets.minimum_baseline_availability,
                    minimum=True,
                ),
                "unlabelled_exclusions": target(
                    metrics["unlabelled_exclusion_fraction"],
                    suite.targets.maximum_unlabelled_exclusion_fraction,
                    minimum=False,
                ),
                "single_spike_detection": target(
                    comparisons["detection_fraction"],
                    suite.targets.minimum_spike_detection_fraction,
                    minimum=True,
                ),
            }
            movement = {}
            for source in EXCHANGE_SOURCES:
                values = [
                    row.value
                    for row in sorted(rows, key=lambda row: (row.timestamp, row.observation_id))
                    if row.source == source and row.timestamp >= case.start
                ]
                movement[source] = {
                    "return_bps": str((values[-1] / values[0] - 1) * 10000) if values else None,
                    "range_bps": str((max(values) - min(values)) / values[0] * 10000)
                    if values
                    else None,
                }
            cases.append(
                {
                    "case": case.model_dump(mode="json"),
                    "observations": counts,
                    "measured_market_movement": movement,
                    "baseline": {
                        "metrics": metrics,
                        "rounds": [round_summary(report) for report in baseline],
                    },
                    "experiments": experiments,
                    "targets": gates,
                }
            )
        code_hashes = {
            name: hashlib.sha256(
                Path(__file__).with_name(name).read_text(encoding="utf-8").encode()
            ).hexdigest()
            for name in ("benchmark.py", "historical.py", "statistics.py", "models.py")
        }
        result = {
            "version": "historical-evaluation-v1",
            "suite_sha256": suite.sha256,
            "policy_sha256": digest(suite.policy.model_dump(mode="json")),
            "suite": suite.model_dump(mode="json"),
            "code_sha256": code_hashes,
            "capture_sha256": dict(sorted(captures.items())),
            "interventions": list(INTERVENTIONS),
            "receipt_model": {
                "kind": "assumed_event_time_plus_delay",
                "delay_ms": suite.receipt_delay_ms,
                "original_receipt_times_available": False,
            },
            "scope": "three_exchange_historical_trades_with_controlled_interventions",
            "heldout_targets_met": all(
                gate["status"] == "pass"
                for case in cases
                if case["case"]["split"] == "holdout"
                for gate in case["targets"].values()
            ),
            "six_provider_validation_complete": False,
            "publication_ready": False,
            "cases": cases,
        }
        return {**result, "report_id": digest(result)}
