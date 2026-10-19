import asyncio
import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from verdict.benchmark import INTERVENTIONS, evaluate_history, historical_rounds, intervene
from verdict.historical import (
    ENDPOINTS,
    HistoricalSuite,
    HistoryCaptureError,
    HistoryFetcher,
    capture_path,
    fetch_history,
    load_capture,
)
from verdict.models import EXCHANGE_SOURCES
from verdict.statistics import AnalysisPolicy


@pytest.fixture
def historical_files(tmp_path):
    start = datetime(2023, 1, 8, 12, tzinfo=UTC)
    suite = HistoricalSuite(
        version="historical-v1",
        description="Unit-test history, not actual exchange evidence",
        receipt_delay_ms=250,
        step_seconds=10,
        policy=AnalysisPolicy(history_seconds=10),
        targets={
            "minimum_baseline_availability": "0.8",
            "maximum_unlabelled_exclusion_fraction": "0.05",
            "minimum_spike_detection_fraction": "0.95",
        },
        cases=[
            {
                "name": "calibration",
                "split": "calibration",
                "regime": "synthetic",
                "start": start,
                "end": start + timedelta(seconds=120),
            },
            {
                "name": "holdout",
                "split": "holdout",
                "regime": "synthetic",
                "start": start + timedelta(days=1),
                "end": start + timedelta(days=1, seconds=120),
            },
        ],
    )
    for case in suite.cases:
        beginning = case.start - timedelta(seconds=40)
        times = [beginning + timedelta(seconds=index) for index in range(161)]
        for source in EXCHANGE_SOURCES:
            if source == "coinbase":
                body = [
                    {
                        "trade_id": index + 1,
                        "price": "100.123456789123456789",
                        "time": stamp.isoformat(),
                        "side": "buy",
                        "size": "1",
                    }
                    for index, stamp in reversed(list(enumerate(times)))
                ]
                params = {"after": "162", "limit": 1000}
            elif source == "kraken":
                body = {
                    "error": [],
                    "result": {
                        "XETHZUSD": [
                            [
                                "100.123456789123456789",
                                "1",
                                str(int(stamp.timestamp())),
                                "b",
                                "l",
                                "",
                                index + 1,
                            ]
                            for index, stamp in enumerate(times)
                        ],
                        "last": str(int(case.end.timestamp()) * 1_000_000_000),
                    },
                }
                params = {
                    "pair": "ETHUSD",
                    "since": str(int(beginning.timestamp()) * 1_000_000_000),
                    "count": 1000,
                }
            else:
                body = [
                    {
                        "a": index + 1,
                        "p": "100.123456789123456789",
                        "T": int(stamp.timestamp() * 1000),
                        "f": index + 1,
                        "l": index + 1,
                    }
                    for index, stamp in enumerate(times[:-1])
                ]
                params = {
                    "symbol": "ETHUSD",
                    "startTime": int(beginning.timestamp() * 1000),
                    "endTime": int(case.end.timestamp() * 1000) - 1,
                    "limit": 1000,
                }
            text = json.dumps(body)
            data = {
                "version": 1,
                "suite_sha256": suite.sha256,
                "case": case.name,
                "source": source,
                "endpoint": ENDPOINTS[source],
                "start": beginning.isoformat(),
                "end": case.end.isoformat(),
                "pages": [
                    {
                        "params": params,
                        "body": text,
                        "body_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "cursor_after": "1" if source == "coinbase" else None,
                        "fetched_at": "2026-10-19T00:00:00Z",
                    }
                ],
            }
            capture_path(tmp_path, case, source).write_bytes(
                gzip.compress(json.dumps(data).encode(), mtime=0)
            )
    path = tmp_path / "suite.json"
    path.write_text(suite.model_dump_json(), encoding="utf-8")
    return suite, path, tmp_path


def read_capture(directory, case, source):
    return json.loads(gzip.decompress(capture_path(directory, case, source).read_bytes()))


def test_historical_capture_preserves_evidence_decimal_and_assumed_receipts(historical_files):
    suite, _, directory = historical_files
    for source in EXCHANGE_SOURCES:
        data = read_capture(directory, suite.cases[0], source)
        rows = load_capture(data, suite, suite.cases[0], source)
        assert len(rows) == 160
        assert rows[0].value == Decimal("100.123456789123456789")
        assert rows[0].received_at - rows[0].timestamp == timedelta(milliseconds=250)
        assert all(row.dataset.startswith("replay:hist-") for row in rows)
        assert rows[0].latency_ms == 0
        assert rows[0].raw_payload


def test_capture_checksum_mismatch_and_wrong_suite_fail(historical_files):
    suite, _, directory = historical_files
    data = read_capture(directory, suite.cases[0], "coinbase")
    data["pages"][0]["body"] += " "
    with pytest.raises(HistoryCaptureError, match="checksum"):
        load_capture(data, suite, suite.cases[0], "coinbase")
    data["suite_sha256"] = "0" * 64
    with pytest.raises(HistoryCaptureError, match="frozen suite"):
        load_capture(data, suite, suite.cases[0], "coinbase")


@pytest.mark.parametrize("source", ["coinbase", "kraken"])
def test_truncated_archives_fail_even_if_body_checksum_is_recomputed(historical_files, source):
    suite, _, directory = historical_files
    data = read_capture(directory, suite.cases[0], source)
    body = json.loads(data["pages"][0]["body"])
    if source == "coinbase":
        body.pop()  # Oldest boundary.
    else:
        body["result"]["XETHZUSD"].pop()  # Newest boundary.
    text = json.dumps(body)
    data["pages"][0].update(body=text, body_sha256=hashlib.sha256(text.encode()).hexdigest())
    with pytest.raises(HistoryCaptureError, match="boundary"):
        load_capture(data, suite, suite.cases[0], source)


def test_full_offline_evaluation_is_repeatable_and_detects_interventions(
    historical_files, monkeypatch
):
    _, path, directory = historical_files
    monkeypatch.setattr(
        httpx.AsyncClient,
        "get",
        AsyncMock(side_effect=AssertionError("offline evaluation contacted network")),
    )
    first = evaluate_history(path, directory)
    with localcontext() as context:
        context.prec = 8
        assert evaluate_history(path, directory) == first
    assert first["heldout_targets_met"] is True
    assert first["six_provider_validation_complete"] is False
    assert first["publication_ready"] is False
    assert first["receipt_model"]["original_receipt_times_available"] is False
    for case in first["cases"]:
        assert case["baseline"]["metrics"]["availability_fraction"] == "1"
        assert case["experiments"]["single_spike"]["paired"]["detection_fraction"] == "1"
        assert case["experiments"]["source_outage"]["paired"]["new_unavailable_rounds"] > 0
        assert case["experiments"]["late_receipt"]["paired"]["new_unavailable_rounds"] > 0
        assert (
            Decimal(
                case["experiments"]["correlated_shift"]["paired"]["maximum_reference_shift_bps"]
            )
            > 3499
        )


def test_missing_venue_is_retained_as_unavailable_and_unscored(historical_files):
    suite, path, directory = historical_files
    for case in suite.cases:
        archive = capture_path(directory, case, "binance_us")
        data = read_capture(directory, case, "binance_us")
        data["pages"][0].update(body="[]", body_sha256=hashlib.sha256(b"[]").hexdigest())
        archive.write_bytes(gzip.compress(json.dumps(data).encode(), mtime=0))
    report = evaluate_history(path, directory)
    assert not report["heldout_targets_met"]
    for case in report["cases"]:
        assert case["observations"]["binance_us"] == 0
        assert case["baseline"]["metrics"]["availability_fraction"] == "0"
        assert case["targets"]["single_spike_detection"]["status"] == "not_scored"
        assert case["targets"]["unlabelled_exclusions"]["status"] == "not_scored"


def test_late_receipts_are_never_seen_before_the_cutoff(historical_files):
    suite, _, directory = historical_files
    case = suite.cases[0]
    rows = [
        row
        for source in EXCHANGE_SOURCES
        for row in load_capture(read_capture(directory, case, source), suite, case, source)
    ]
    spec = next(spec for spec in INTERVENTIONS if spec["name"] == "late_receipt")
    changed = intervene(rows, case, spec)
    reports = historical_rounds(changed, suite, case, "late_receipt")
    by_id = {row.observation_id: row for row in changed}
    for report in reports:
        assert all(
            by_id[identity].received_at <= report.cutoff
            for identity in report.input_observation_ids
        )
    assert all(row.dataset == f"replay:hist-{case.name}" for row in rows)
    assert all("benchmark_intervention" not in row.raw_payload for row in rows)


@pytest.mark.parametrize(
    "kind", ["overlap", "reversed", "missing_holdout", "duplicate", "missing_source"]
)
def test_suite_rejects_leaking_or_invalid_splits(historical_files, kind):
    suite, _, _ = historical_files
    payload = suite.model_dump(mode="json")
    if kind == "overlap":
        payload["cases"][1]["start"] = (suite.cases[0].end + timedelta(seconds=10)).isoformat()
        payload["cases"][1]["end"] = (suite.cases[0].end + timedelta(seconds=130)).isoformat()
    elif kind == "reversed":
        payload["cases"][0]["split"], payload["cases"][1]["split"] = "holdout", "calibration"
    elif kind == "missing_holdout":
        payload["cases"][1]["split"] = "calibration"
    elif kind == "duplicate":
        payload["cases"][1]["name"] = payload["cases"][0]["name"]
    else:
        payload["policy"]["sources"] = ["coinbase", "kraken"]
    with pytest.raises(ValueError):
        HistoricalSuite.model_validate(payload)


def test_coinbase_binary_discovery_and_contiguous_pagination():
    beginning = datetime(2023, 1, 1, tzinfo=UTC)

    def handle(request):
        after = request.url.params.get("after")
        last = int(after) - 1 if after else 1000
        limit = int(request.url.params["limit"])
        body = [
            {
                "trade_id": identity,
                "price": "100",
                "time": (beginning + timedelta(seconds=identity)).isoformat(),
            }
            for identity in range(last, max(0, last - limit), -1)
        ]
        return httpx.Response(200, json=body, headers={"CB-AFTER": str(body[-1]["trade_id"])})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            fetcher = HistoryFetcher(client, "coinbase", sleep=AsyncMock())
            await fetcher.coinbase(
                beginning + timedelta(seconds=100), beginning + timedelta(seconds=200)
            )
            assert fetcher.requests < 20
            assert json.loads(fetcher.pages[-1]["body"])[0]["trade_id"] == 200

    asyncio.run(run())


def test_request_timeouts_retry_with_same_parameters_and_respect_budget():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(200, json=[])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            fetcher = HistoryFetcher(client, "binance_us", sleep=AsyncMock(), max_requests=2)
            await fetcher.get({"symbol": "ETHUSD"})
            assert calls[0] == calls[1]
            with pytest.raises(HistoryCaptureError, match="budget"):
                await fetcher.get({"symbol": "ETHUSD"})

    asyncio.run(run())


def test_kraken_stuck_cursor_does_not_claim_complete_history():
    start = datetime(2023, 1, 1, tzinfo=UTC)
    timestamp = str(int(start.timestamp()))

    def handle(request):
        return httpx.Response(
            200,
            json={
                "error": [],
                "result": {
                    "XETHZUSD": [["100", "1", timestamp, "b", "l", "", 1]],
                    "last": request.url.params["since"],
                },
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(HistoryCaptureError, match="advancing"):
                await HistoryFetcher(client, "kraken", sleep=AsyncMock()).kraken(
                    start, start + timedelta(seconds=120)
                )

    asyncio.run(run())


def test_binance_aggregate_pagination_handles_same_millisecond_without_loss():
    start = datetime(2023, 1, 1, tzinfo=UTC)
    stamp = int(start.timestamp() * 1000)
    calls = []

    def handle(request):
        calls.append(dict(request.url.params))
        first, last = (1, 1001) if "startTime" in request.url.params else (1001, 1003)
        return httpx.Response(
            200, json=[{"a": i, "p": "100", "T": stamp, "f": i, "l": i} for i in range(first, last)]
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            fetcher = HistoryFetcher(client, "binance_us", sleep=AsyncMock())
            await fetcher.binance_us(start, start + timedelta(seconds=120))
            assert calls[1]["fromId"] == "1001"
            assert len(fetcher.pages) == 2

    asyncio.run(run())


def test_fetch_resumes_existing_captures_without_network_or_overwrite(
    historical_files, monkeypatch
):
    _, path, directory = historical_files
    original = {item.name: item.read_bytes() for item in directory.glob("*.gz")}
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(side_effect=AssertionError("network")))
    outcomes = asyncio.run(fetch_history(path, directory))
    assert all(result["retained"] for result in outcomes)
    assert {item.name: item.read_bytes() for item in directory.glob("*.gz")} == original


def test_history_cli_evaluation_requires_no_database(historical_files, monkeypatch, capsys):
    from verdict.cli import main

    _, suite_path, directory = historical_files
    output = directory / "report.json"
    monkeypatch.setattr("verdict.cli.build_engine", lambda _: pytest.fail("opened database"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "history",
            "evaluate",
            str(suite_path),
            str(directory),
            "--output",
            str(output),
        ],
    )
    main()
    assert (
        json.loads(capsys.readouterr().out)["report_id"]
        == json.loads(output.read_text())["report_id"]
    )


def test_frozen_suite_and_real_archives_remain_readable():
    from verdict.historical import load_suite

    path = Path("benchmarks/historical-v1.json")
    suite = load_suite(path)
    count = 0
    for case in suite.cases:
        for source in EXCHANGE_SOURCES:
            data = read_capture(Path("benchmarks/captures"), case, source)
            rows = load_capture(data, suite, case, source)
            assert all(row.received_at >= row.timestamp for row in rows)
            count += len(rows)
    assert count > 1000
