import asyncio
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal, localcontext
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import update

from verdict.api import create_app
from verdict.cli import main
from verdict.config import Settings
from verdict.dependencies import bundled_registry
from verdict.models import SOURCES
from verdict.replay import load_observations
from verdict.scoring import ScoringPolicy, ScoringReport, build_scoring_report, score_observations
from verdict.storage import ObservationStore, ScoringRoundRow, build_engine

BASE = datetime(2026, 10, 16, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def rows():
    templates = {
        row.source: row for row in load_observations(ROOT / "simulations/six-provider.json")
    }

    def make(
        times=range(10, 601, 10),
        sources=SOURCES,
        *,
        price=None,
        received=None,
        dataset="replay:scoring",
    ):
        return [
            templates[source].model_copy(
                update={
                    "dataset": dataset,
                    "provider_event_id": f"{source}-{time}",
                    "timestamp": BASE + timedelta(seconds=time),
                    "received_at": BASE
                    + timedelta(seconds=received(source, time) if received else time),
                    "event_sequence": time + 10000,
                    "value": Decimal(price(source, time) if price else "100"),
                    "raw_payload": {"fixture": "synthetic-scoring", "source": source, "time": time},
                }
            )
            for time in times
            for source in sources
        ]

    return make


def score(observations, at=600, policy=None, dataset="replay:scoring", registry=None):
    return score_observations(
        observations,
        dataset=dataset,
        cutoff=BASE + timedelta(seconds=at),
        registry=registry or bundled_registry(),
        policy=policy,
    )


def source(report, name="coinbase"):
    return next(item for item in report.sources if item.source == name)


def short_policy(window=30, **updates):
    return ScoringPolicy(
        window_seconds=window,
        minimum_fresh_samples=1,
        minimum_comparisons=1,
        minimum_unique_observations=1,
        **updates,
    )


def test_established_history_keeps_breakdown_and_pinned_unknowns(rows):
    report = score(rows())
    assert len(report.ticks) == 60
    for item in report.sources:
        assert item.scheduled_samples == item.fresh_samples == item.comparisons == 60
        assert item.inlier_samples == item.unique_fresh_observations == 60
        assert item.consistency_bps == item.uptime_bps == item.mean_freshness_bps == 10000
        assert item.score_bps == item.uncapped_score_bps == 10000
        assert item.status == "established"
        assert not item.cold_start
        assert item.unknown_dependencies
        assert item.reasons == ("upstream_dependencies_unresolved",)
    assert report.registry_id == bundled_registry().registry_id
    assert report.registry_review_after_cutoff
    assert report.confidence is report.independent_domain_count is None
    assert not report.publication_ready
    assert report.window_start_exclusive == BASE
    assert report.cutoff == BASE + timedelta(seconds=600)
    assert ScoringReport.model_validate_json(report.model_dump_json()) == report


def test_empty_and_cold_start_history_does_not_invent_health(rows):
    empty = score([])
    assert all(item.score_bps == 0 and item.status == "unavailable" for item in empty.sources)
    assert all(
        item.consistency_bps is None and item.missing_samples == 60 for item in empty.sources
    )
    one = score(rows(times=[600]))
    item = source(one)
    assert item.fresh_samples == item.comparisons == item.unique_fresh_observations == 1
    assert item.uptime_bps == item.mean_freshness_bps == item.score_bps == 166
    assert item.status == "cold_start"
    assert item.cold_start
    assert "insufficient_fresh_history" in item.reasons
    assert "insufficient_comparison_history" in item.reasons
    assert "insufficient_unique_observations" in item.reasons


def test_repeated_trade_does_not_mature_and_cold_start_cap_applies(rows):
    policy = short_policy().model_copy(update={"minimum_unique_observations": 2})
    observations = rows(times=[0])
    report = score(observations * 4, at=30, policy=policy)
    item = source(report)
    assert item.unique_fresh_observations == 1
    assert item.fresh_samples == item.comparisons == 3
    assert item.consistency_bps == 10000
    assert item.current_freshness_bps == 3678
    assert item.uncapped_score_bps == 3678
    assert item.score_bps == 2500
    assert score(observations, at=30, policy=policy).report_id == report.report_id


def test_source_cannot_compare_with_itself_or_invent_comparison_without_peers(rows):
    three = score(rows(sources=("coinbase", "kraken", "binance_us")))
    for name in ("coinbase", "kraken", "binance_us"):
        item = source(three, name)
        assert item.comparisons == 0
        assert item.consistency_bps is item.score_bps is None
        assert item.status == "unscored"
    four = score(rows(sources=("coinbase", "kraken", "binance_us", "pyth")))
    assert source(four).score_bps == 10000
    for tick in four.ticks:
        for sample in tick.sources:
            if sample.comparison != "unscored":
                assert len(sample.peer_observation_ids) == 3
                assert sample.observation_id not in sample.peer_observation_ids


def test_isolated_outlier_and_historical_failure_affect_scores(rows):
    observations = rows(price=lambda s, t: "135" if s == "coinbase" and t <= 200 else "100")
    report = score(observations)
    item = source(report)
    assert (item.outlier_samples, item.inlier_samples, item.comparisons) == (20, 40, 60)
    assert item.consistency_bps == item.score_bps == 6666
    assert source(report, "kraken").score_bps == 10000
    current_attack = score(
        rows(price=lambda s, t: "135" if s == "coinbase" and t == 600 else "100")
    )
    assert source(current_attack).consistency_bps == 9833
    assert source(current_attack).score_bps == 0
    assert "current_outlier" in source(current_attack).reasons


def test_coordinated_move_and_correlated_majority_show_agreement_limits(rows):
    market_move = score(rows(price=lambda s, t: "110" if t >= 300 else "100"))
    assert all(item.score_bps == 10000 for item in market_move.sources)
    majority = score(rows(price=lambda s, t: "100" if s in ("coinbase", "kraken") else "135"))
    assert source(majority).score_bps == 0
    assert source(majority, "pyth").score_bps == 10000
    assert not majority.publication_ready
    assert majority.confidence is None


def test_peer_mad_and_zero_mad_floor_use_strict_boundary(rows):
    on = score(
        rows(times=[10], price=lambda s, t: "101" if s == "coinbase" else "100"),
        at=10,
        policy=short_policy(10),
    )
    assert source(on).outlier_samples == 0
    above = score(
        rows(times=[10], price=lambda s, t: "101.000000000000001" if s == "coinbase" else "100"),
        at=10,
        policy=short_policy(10),
    )
    assert source(above).outlier_samples == 1
    prices = dict(zip(sorted(SOURCES), ["100", "102", "104", "106", "108", "150"], strict=True))
    varying = score(rows(times=[10], price=lambda s, t: prices[s]), at=10, policy=short_policy(10))
    sample = next(s for s in varying.ticks[0].sources if s.source == "uniswap_v3")
    assert sample.peer_median == 104
    assert sample.deviation_threshold == 12
    assert sample.comparison == "outlier"


def test_outage_counts_every_missing_or_stale_grid_sample_and_recovers_on_new_evidence(rows):
    observations = rows(times=range(10, 301, 10))
    report = score(observations)
    item = source(report)
    assert (item.fresh_samples, item.stale_samples, item.missing_samples) == (33, 27, 0)
    assert item.uptime_bps == 5500
    assert item.score_bps == 0
    assert item.status == "unavailable"
    assert item.current_freshness_bps == 0
    assert item.consistency_bps == 10000
    recovered = score([*observations, *rows(times=[600])])
    assert source(recovered).score_bps < 6000
    # No saved statistical rounds or HTTP-success counters enter this calculation.
    assert source(recovered).fresh_samples == 34


def test_current_peer_outage_cannot_borrow_historical_consistency(rows):
    observations = [
        row
        for row in rows()
        if row.source == "coinbase" or row.timestamp <= BASE + timedelta(seconds=560)
    ]
    item = source(score(observations))
    assert item.consistency_bps == 10000
    assert item.score_bps is item.uncapped_score_bps is None
    assert item.status == "unscored"
    assert "current_insufficient_peers" in item.reasons


def test_received_time_event_time_and_out_of_order_boundaries_match_sql(store, rows):
    registry = bundled_registry()
    store.save_dependency_registry(registry)
    observations = [
        *rows(times=[-60, -50, 10, 20]),
        *rows(times=[-40], received=lambda s, t: 15),
        *rows(times=[25], received=lambda s, t: 5),
        *rows(times=[29], received=lambda s, t: 31),
        *rows(times=[40], received=lambda s, t: 20),
    ]
    store.add_many(observations)
    policy = short_policy()
    direct = score(observations, at=30, policy=policy)
    stored = build_scoring_report(
        store,
        dataset="replay:scoring",
        cutoff=BASE + timedelta(seconds=30),
        registry_id=registry.registry_id,
        policy=policy,
    )
    assert direct == stored
    assert source(stored).clock_skew_samples == 1
    assert source(stored).score_bps == 0
    assert [tick.sources[0].value for tick in stored.ticks] == [100, 100, 100]
    ids = {
        row.observation_id
        for row in observations
        if row.timestamp in (BASE + timedelta(seconds=29), BASE + timedelta(seconds=40))
    }
    assert not ids.intersection(stored.input_observation_ids)


def test_old_late_receipt_can_replace_pre_window_anchor(store, rows):
    registry = bundled_registry()
    store.save_dependency_registry(registry)
    observations = [*rows(times=[-60]), *rows(times=[-40], received=lambda s, t: 20)]
    store.add_many(observations)
    policy = short_policy()
    stored = build_scoring_report(
        store,
        dataset="replay:scoring",
        cutoff=BASE + timedelta(seconds=30),
        registry_id=registry.registry_id,
        policy=policy,
    )
    assert stored == score(observations, at=30, policy=policy)
    assert stored.ticks[0].sources[0].age_seconds == 70
    assert stored.ticks[1].sources[0].age_seconds == 60
    assert len(stored.input_observation_ids) == 12


def test_source_timing_is_explicit_and_versioned(rows):
    original = short_policy(window=40)
    timings = tuple(
        timing.model_copy(update={"freshness_seconds": 40})
        if timing.source == "chainlink"
        else timing
        for timing in original.source_timing
    )
    customized = original.model_copy(update={"source_timing": timings})
    before = score(rows(times=[0]), at=40, policy=original)
    after = score(rows(times=[0]), at=40, policy=customized)
    assert source(before, "chainlink").fresh_samples == 3
    assert source(after, "chainlink").fresh_samples == 4
    assert source(after, "coinbase").fresh_samples == 3
    assert before.policy_sha256 != after.policy_sha256


def test_deterministic_order_precision_cutoff_alignment_and_namespace(rows):
    observations = rows(times=[5, 15, 25])
    expected = score(observations, at=30, policy=short_policy())
    assert score(list(reversed(observations)), at=39.999, policy=short_policy()) == expected
    with localcontext() as context:
        context.prec = 8
        context.rounding = ROUND_UP
        assert score(observations, at=30, policy=short_policy()) == expected
    assert (
        score([*observations, *rows(times=[20], dataset="live")], at=30, policy=short_policy())
        == expected
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        score_observations(
            [], dataset="live", cutoff=datetime(2026, 10, 16), registry=bundled_registry()
        )
    with pytest.raises(ValidationError):
        score([], dataset="../live")


def test_event_sequence_and_identity_break_timestamp_ties(store, rows):
    observations = rows(times=[10], sources=("coinbase",))
    first = observations[0]
    higher = first.model_copy(
        update={"provider_event_id": "higher", "event_sequence": 99999, "value": Decimal("101")}
    )
    equal = higher.model_copy(update={"provider_event_id": "equal", "value": Decimal("102")})
    candidates = [first, higher, equal]
    store.add_many(candidates)
    registry = bundled_registry()
    store.save_dependency_registry(registry)
    expected = max(candidates, key=lambda row: (row.event_sequence, row.observation_id))
    report = build_scoring_report(
        store,
        dataset="replay:scoring",
        cutoff=BASE + timedelta(seconds=30),
        registry_id=registry.registry_id,
        policy=short_policy(),
    )
    assert report == score(candidates, at=30, policy=short_policy())
    assert report.input_observation_ids == (expected.observation_id,)


def test_limits_and_conflicting_evidence_fail_closed(store, rows):
    registry = bundled_registry()
    store.save_dependency_registry(registry)
    observations = rows(times=[10, 20])
    policy = short_policy().model_copy(update={"max_observations": 6})
    with pytest.raises(ValueError, match="limit exceeded"):
        score(observations, at=30, policy=policy)
    store.add_many(observations)
    with pytest.raises(ValueError, match="limit exceeded"):
        build_scoring_report(
            store,
            dataset="replay:scoring",
            cutoff=BASE + timedelta(seconds=30),
            registry_id=registry.registry_id,
            policy=policy,
        )
    with pytest.raises(ValueError, match="conflicting"):
        score([observations[0], observations[0].model_copy(update={"value": Decimal("999")})])
    assert store.scoring_rounds() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"step_seconds": 0},
        {"window_seconds": 601},
        {"window_seconds": 86400},
        {"window_seconds": 10},
        {"minimum_peers": 2},
        {"cold_start_cap_bps": 10001},
        {"minimum_unique_observations": True},
        {"source_timing": []},
        {"source_timing": [dict(source="coinbase")] * 6},
    ],
)
def test_invalid_scoring_policy_is_rejected(changes):
    with pytest.raises(ValidationError):
        ScoringPolicy.model_validate({**ScoringPolicy().model_dump(mode="json"), **changes})


def test_persistence_requires_registry_and_verifies_hashes(store, rows):
    report = score(rows(times=[10, 20, 30]), at=30, policy=short_policy())
    with pytest.raises(ValueError, match="registry must be retained"):
        store.save_scoring_round(report)
    store.save_dependency_registry(bundled_registry())
    with pytest.raises(ValueError, match="input evidence is missing"):
        store.save_scoring_round(report)
    assert store.scoring_rounds(dataset=report.dataset) == []
    store.add_many(rows(times=[10, 20, 30]))
    assert store.save_scoring_round(report)
    assert not store.save_scoring_round(report)
    with pytest.raises(ValueError, match="checksum mismatch"):
        store.save_scoring_round(report.model_copy(update={"input_sha256": "0" * 64}))
    with pytest.raises(ValueError, match="policy checksum"):
        ScoringReport.model_validate(report.model_copy(update={"policy_sha256": "0" * 64}))
    reopened = build_engine(str(store.engine.url))
    try:
        other = ObservationStore(reopened)
        assert other.get_scoring_round(
            report.report_id, dataset=report.dataset
        ) == report.model_dump(mode="json")
        assert other.get_scoring_round(report.report_id) is None
        assert other.scoring_rounds() == []
    finally:
        reopened.dispose()
    with store.session() as session, session.begin():
        session.execute(update(ScoringRoundRow).values(registry_id="0" * 64))
    with pytest.raises(ValueError, match="metadata mismatch"):
        store.get_scoring_round(report.report_id, dataset=report.dataset)


def test_concurrent_scoring_registration_and_version_pinning(store, rows):
    registry = bundled_registry()
    store.save_dependency_registry(registry)
    store.add_many(rows(times=[10, 20, 30]))
    report = score(rows(times=[10, 20, 30]), at=30, policy=short_policy())

    async def run():
        return await asyncio.gather(
            *(asyncio.to_thread(store.save_scoring_round, report) for _ in range(4))
        )

    assert sorted(asyncio.run(run())) == [False, False, False, True]
    later_registry = registry.model_copy(update={"version": "dependencies-new-review"})
    store.save_dependency_registry(later_registry)
    later = score(rows(times=[10, 20, 30]), at=30, policy=short_policy(), registry=later_registry)
    assert later.report_id != report.report_id
    assert store.get_scoring_round(report.report_id, dataset=report.dataset) == report.model_dump(
        mode="json"
    )


def test_cli_persists_scores_and_api_is_read_only_and_dataset_scoped(
    store, rows, monkeypatch, capsys
):
    registry = bundled_registry()
    store.save_dependency_registry(registry)
    store.add_many(rows())
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "score",
            "--dataset",
            "replay:scoring",
            "--at",
            "2026-10-16T00:10:09Z",
            "--registry-id",
            registry.registry_id,
        ],
    )
    main()
    report = ScoringReport.model_validate_json(capsys.readouterr().out)
    main()
    assert ScoringReport.model_validate_json(capsys.readouterr().out) == report
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        path = f"/v1/scores/{report.report_id}"
        assert client.get(path).status_code == 404
        assert client.get("/v1/scores").json() == []
        assert client.get(path, params={"dataset": report.dataset}).json() == report.model_dump(
            mode="json"
        )
        assert client.get("/v1/scores", params={"dataset": report.dataset}).json() == [
            report.model_dump(mode="json")
        ]
        assert client.get("/v1/scores", params={"limit": 101}).status_code == 422
        assert client.get("/v1/scores/invalid").status_code == 422
        assert client.post(path, json={}).status_code == 405
        assert client.get("/v1/feed/ETH-USD").status_code == 503
        assert all(item["trust_score"] is None for item in client.get("/v1/sources").json())
        assert store.rounds(dataset=report.dataset) == []


def test_cli_policy_loading_and_invalid_requests(store, rows, monkeypatch, capsys, tmp_path):
    registry = bundled_registry()
    store.save_dependency_registry(registry)
    store.add_many(rows(times=[10, 20, 30]))
    path = tmp_path / "scoring.json"
    path.write_text(short_policy().model_dump_json(), encoding="utf-8")
    base = ["verdict", "score", "--registry-id", registry.registry_id]
    monkeypatch.setattr(
        "sys.argv",
        [
            *base,
            "--dataset",
            "replay:scoring",
            "--at",
            "2026-10-16T00:00:30Z",
            "--policy",
            str(path),
        ],
    )
    main()
    assert ScoringReport.model_validate_json(capsys.readouterr().out).policy == short_policy()
    for options in (
        ["--dataset", "replay:scoring"],
        ["--dataset", "../live"],
        ["--at", "2026-10-16T00:00:30"],
        ["--registry-id", "0" * 64],
    ):
        monkeypatch.setattr("sys.argv", [*base, *options])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2


def test_upgrade_from_0006_preserves_existing_evidence(store, rows):
    config = Config(str(ROOT / "alembic.ini"))
    command.downgrade(config, "0006")
    registry = bundled_registry()
    store.save_dependency_registry(registry)
    observations = rows(times=[10, 20, 30])
    store.add_many(observations)
    with TestClient(create_app(Settings(_env_file=None), store)) as client:
        assert client.get("/health/ready").status_code == 503
    command.upgrade(config, "head")
    command.check(config)
    store.check_ready()
    report = build_scoring_report(
        store,
        dataset="replay:scoring",
        cutoff=BASE + timedelta(seconds=30),
        registry_id=registry.registry_id,
        policy=short_policy(),
    )
    assert report == score(observations, at=30, policy=short_policy())
    assert store.save_scoring_round(report)


def test_missing_peer_history_limits_otherwise_perfect_score(rows):
    observations = [
        row
        for row in rows()
        if row.source == "coinbase" or row.timestamp >= BASE + timedelta(seconds=310)
    ]
    item = source(score(observations))
    assert item.consistency_bps == item.uptime_bps == item.mean_freshness_bps == 10000
    assert item.comparisons == 30
    assert item.comparison_coverage_bps == item.score_bps == 5000
    assert not item.cold_start


def test_fractional_freshness_and_clock_skew_boundaries(rows):
    observations = rows(times=[0])
    at_boundary = score(observations, at=30, policy=short_policy())
    assert source(at_boundary).fresh_samples == 3
    older = [
        row.model_copy(
            update={
                "timestamp": row.timestamp - timedelta(microseconds=1),
                "received_at": row.received_at - timedelta(microseconds=1),
            }
        )
        for row in observations
    ]
    assert source(score(older, at=30, policy=short_policy())).stale_samples == 1
    valid = rows(times=[10], received=lambda s, t: 5)
    assert source(score(valid, at=30, policy=short_policy())).clock_skew_samples == 0
    invalid = [
        row.model_copy(update={"received_at": row.received_at - timedelta(microseconds=1)})
        for row in valid
    ]
    assert source(score(invalid, at=30, policy=short_policy())).clock_skew_samples == 3
