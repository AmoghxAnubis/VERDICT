from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest

from verdict.models import SOURCES
from verdict.statistics import AnalysisPolicy, analyze, build_report

BASE = datetime(2026, 10, 16, tzinfo=UTC)


def evaluate(rows, at=10, policy=None):
    return analyze(rows, cutoff=BASE + timedelta(seconds=at), dataset="replay:test", policy=policy)


def test_healthy_three_source_round_has_explanations(market_observation):
    rows = [
        market_observation(source, price)
        for source, price in zip(SOURCES, ["100", "100.1", "99.9"], strict=True)
    ]
    report = evaluate(rows)
    assert report.status == "analyzed"
    assert report.median_reference == Decimal("100")
    assert report.mad == Decimal("0.1")
    assert report.fresh_sources == report.inlier_sources == 3
    assert not report.publication_ready
    assert all(not row.exclusion_reasons for row in report.sources)


def test_zero_mad_still_rejects_extreme_source(market_observation):
    rows = [
        market_observation(source, price)
        for source, price in zip(SOURCES, ["100", "100", "135"], strict=True)
    ]
    report = evaluate(rows)
    assert report.mad == 0
    assert report.outlier_threshold == 1
    assert report.sources[2].exclusion_reasons == ["extreme_deviation"]
    assert report.inlier_sources == 2
    assert report.status == "insufficient_data"


def test_zero_mad_ignores_small_price_tick(market_observation):
    rows = [
        market_observation(source, price)
        for source, price in zip(SOURCES, ["100", "100", "100.01"], strict=True)
    ]
    assert evaluate(rows).status == "analyzed"


def test_market_wide_jump_is_signal_not_automatic_exclusion(market_observation):
    rows = [
        market_observation(source, price, time)
        for source in SOURCES
        for price, time in [("100", 0), ("110", 1)]
    ]
    report = evaluate(rows, at=2)
    assert report.status == "analyzed"
    assert report.median_reference == 110
    assert all(row.signals == ["high_price_velocity"] for row in report.sources)
    assert all(row.exclusion_reasons == [] for row in report.sources)


def test_fewer_than_three_sources_does_not_invent_a_baseline(market_observation):
    report = evaluate([market_observation()])
    assert report.median_reference is None
    assert report.sources[0].exclusion_reasons == ["insufficient_sources"]
    assert report.sources[1].exclusion_reasons == ["missing"]


def test_stale_feed_and_future_clock_are_excluded(market_observation):
    rows = [
        market_observation("coinbase", time=0),
        market_observation("kraken", time=35, received=0),
        market_observation("binance_us", time=39),
    ]
    report = evaluate(rows, at=40)
    assert report.sources[0].exclusion_reasons == ["stale"]
    assert report.sources[1].exclusion_reasons == ["clock_skew"]
    assert report.sources[2].exclusion_reasons == ["insufficient_sources"]


def test_twap_uses_time_and_reports_gap_coverage(market_observation):
    rows = [market_observation(price="100", time=0), market_observation(price="120", time=5)]
    source = evaluate(rows, policy=AnalysisPolicy(history_seconds=10)).sources[0]
    assert source.twap == 110
    assert source.twap_coverage_seconds == 10
    assert source.twap_coverage_fraction == 1
    assert source.velocity_bps_per_second == 400
    assert source.rms_return_bps == 2000


def test_twap_does_not_carry_price_through_outage(market_observation):
    report = evaluate([market_observation()], at=60, policy=AnalysisPolicy(history_seconds=60))
    assert report.sources[0].twap_coverage_seconds == 30
    assert report.sources[0].twap_coverage_fraction == Decimal("0.5")


def test_received_after_cutoff_and_future_events_are_not_visible(store, market_observation):
    first = market_observation(time=5)
    late = market_observation(price="150", time=8, received=12)
    future = market_observation(price="200", time=12, received=9)
    store.add_many([future, late, first])
    report = build_report(store, dataset="replay:test", cutoff=BASE + timedelta(seconds=10))
    assert report.input_observation_ids == [first.observation_id]
    assert report.sources[0].value == 100
    assert evaluate([first, late, future]).round_id == report.round_id


def test_round_is_order_independent_and_ignores_other_datasets(market_observation):
    rows = [market_observation(source) for source in SOURCES]
    expected = evaluate(rows)
    assert evaluate(list(reversed(rows))).round_id == expected.round_id
    other = market_observation(price="1000", dataset="live")
    assert evaluate([*rows, other]).round_id == expected.round_id
    with localcontext() as context:
        context.prec = 8
        assert evaluate(rows).round_id == expected.round_id
    assert evaluate(rows, policy=AnalysisPolicy(mad_multiplier=7)).round_id != expected.round_id


def test_snapshot_refuses_silent_truncation(store, market_observation):
    store.add_many([market_observation(time=i) for i in range(4)])
    with pytest.raises(ValueError, match="limit exceeded"):
        build_report(
            store,
            dataset="replay:test",
            cutoff=BASE + timedelta(seconds=10),
            policy=AnalysisPolicy(max_observations=3),
        )


def test_round_persistence_is_immutable_and_dataset_scoped(store, market_observation):
    report = evaluate([market_observation(source) for source in SOURCES])
    assert store.save_round(report)
    assert not store.save_round(report)
    assert store.rounds() == []
    assert store.get_round(report.round_id) is None
    assert store.get_round(report.round_id, dataset="replay:test") == report.model_dump(mode="json")
    forged = report.model_copy(update={"median_reference": Decimal("200")})
    with pytest.raises(ValueError, match="identity conflict"):
        store.save_round(forged)


def test_naive_cutoff_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        analyze([], cutoff=datetime(2026, 10, 16), dataset="live")
