import json
import sys
from datetime import timedelta
from decimal import ROUND_UP, localcontext
from pathlib import Path

import pytest
from pydantic import ValidationError

from verdict import adversarial
from verdict.cli import main
from verdict.dependencies import checksum

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "benchmarks/adversarial-v1.json"


@pytest.fixture(scope="module")
def report():
    return adversarial.evaluate_adversarial(SUITE)


def test_full_pipeline_never_publishes_even_with_high_quality(report):
    assert report["report_id"] == checksum({k: v for k, v in report.items() if k != "report_id"})
    count = 0
    for profile in report["profiles"].values():
        for case in profile["cases"].values():
            for row in case["rounds"]:
                count += 1
                assert row["status"] == "unavailable"
                assert row["value"] is row["confidence"] is None
                assert not row["publication_ready"]
                gate = next(g for g in row["gates"] if g["name"] == "independent_domains")
                assert not gate["passed"] and gate["observed"] is None
    assert count == 400
    baseline = report["profiles"]["synthetic"]["cases"]["baseline"]["rounds"]
    assert baseline[-1]["quality_score_bps"] == 9700
    assert baseline[-1]["total_weight_bps"] == 9996
    for case in report["profiles"]["bundled"]["cases"].values():
        assert all(
            row["candidate_value"] is None and row["total_weight_bps"] == 0
            for row in case["rounds"]
        )


def test_single_attack_quarantine_and_slow_recovery(report):
    case = report["profiles"]["synthetic"]["cases"]["single_spike"]
    assert case["paired"]["maximum_candidate_shift_bps"] == 1
    assert case["paired"]["first_quarantine_delay_seconds"] == {"coinbase": 0}
    assert case["paired"]["first_positive_weight_after_intervention_seconds"] == {"coinbase": 50}
    for row in case["rounds"]:
        if 120 <= row["seconds"] < 180:
            source = row["sources"]["coinbase"]
            assert source["state"] == "quarantined" and source["weight_bps"] == 0
            assert source["comparison"] == "outlier"


def test_market_move_and_correlated_majority_are_distinct(report):
    cases = report["profiles"]["synthetic"]["cases"]
    assert cases["market_move"]["paired"]["additional_quarantine_decisions"] == 0
    assert cases["market_move"]["paired"]["maximum_candidate_shift_bps"] == 1000
    assert cases["correlated_pair"]["paired"]["maximum_candidate_shift_bps"] == 0
    assert cases["correlated_majority"]["paired"]["maximum_candidate_shift_bps"] == 3500
    assert report["attack_targets"]["correlated_majority"]["status"] == "fail"
    assert not report["attack_targets_met"]


@pytest.mark.parametrize(
    ("name", "sources"),
    [
        ("source_outage", ["coinbase"]),
        ("shared_outage", ["chainlink", "uniswap_v3"]),
        ("late_receipt", ["kraken"]),
    ],
)
def test_missing_or_delayed_data_waits_for_freshness_expiry(report, name, sources):
    paired = report["profiles"]["synthetic"]["cases"][name]["paired"]
    assert paired["first_quarantine_delay_seconds"] == dict.fromkeys(sources, 20)
    assert paired["first_positive_weight_after_intervention_seconds"] == dict.fromkeys(sources, 50)


def test_late_receipts_and_future_events_are_never_selected(report):
    suite = adversarial.AdversarialSuite.model_validate(report["suite"])
    templates = adversarial.load_observations(SUITE.parent / suite.fixture)
    rows = adversarial.observations(
        templates, suite, "late_receipt", adversarial.CASES["late_receipt"], "synthetic"
    )
    retained = {row.observation_id: row for row in rows}
    for tick in report["profiles"]["synthetic"]["cases"]["late_receipt"]["rounds"]:
        cutoff = adversarial.BASE + timedelta(seconds=tick["seconds"])
        for source in tick["sources"].values():
            row = retained[source["observation_id"]]
            assert row.timestamp <= cutoff and row.received_at <= cutoff


def test_repeat_under_changed_decimal_context_and_input_order(report, monkeypatch):
    original = adversarial.load_observations
    monkeypatch.setattr(
        adversarial, "load_observations", lambda path: list(reversed(original(path)))
    )
    with localcontext() as context:
        context.prec = 4
        context.rounding = ROUND_UP
        repeated = adversarial.evaluate_adversarial(SUITE)
    assert repeated == report


@pytest.mark.parametrize(
    "changes",
    [
        {"attack_start_seconds": 60},
        {"attack_end_seconds": 181},
        {"first_round_seconds": 50},
        {"last_round_seconds": 4000},
    ],
)
def test_invalid_temporal_suites_are_rejected(changes):
    data = json.loads(SUITE.read_text()) | changes
    with pytest.raises(ValidationError):
        adversarial.AdversarialSuite.model_validate(data)


def test_fixture_hash_and_provider_coverage_are_checked(tmp_path, monkeypatch):
    data = json.loads(SUITE.read_text())
    data["fixture"] = str(ROOT / "simulations/six-provider.json")
    data["fixture_sha256"] = "0" * 64
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="checksum"):
        adversarial.evaluate_adversarial(path)
    original = adversarial.load_observations
    monkeypatch.setattr(adversarial, "load_observations", lambda path: original(path) * 2)
    with pytest.raises(ValueError, match="exactly one"):
        adversarial.evaluate_adversarial(SUITE)


@pytest.mark.parametrize("strict", [False, True])
def test_cli_writes_reviewable_report_before_strict_failure(report, tmp_path, monkeypatch, strict):
    output = tmp_path / "result.json"
    monkeypatch.setattr(adversarial, "evaluate_adversarial", lambda path: report)
    # Benchmark execution must not open storage or load runtime credentials.
    monkeypatch.setattr("verdict.cli.get_settings", lambda: pytest.fail("settings loaded"))
    args = ["verdict", "benchmark", str(SUITE), "--output", str(output)]
    if strict:
        args.append("--require-targets")
    monkeypatch.setattr(sys, "argv", args)
    if strict:
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
    else:
        main()
    assert json.loads(output.read_text()) == report
