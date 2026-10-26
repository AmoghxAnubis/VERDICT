import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from pydantic import TypeAdapter

from verdict.aggregation import build_aggregation_report
from verdict.confidence import ConfidencePolicy, build_confidence_report
from verdict.config import get_settings
from verdict.dependencies import DependencyRegistry, bundled_registry, inspect_dependencies
from verdict.ingestion import ingest_all, ingest_source
from verdict.models import SOURCES, Dataset
from verdict.replay import replay
from verdict.scoring import ScoringPolicy, build_scoring_report
from verdict.statistics import AnalysisPolicy, build_report
from verdict.storage import ObservationStore, build_engine
from verdict.weighting import WeightingPolicy


def main() -> None:
    parser = argparse.ArgumentParser(prog="verdict")
    commands = parser.add_subparsers(dest="command", required=True)
    from verdict.signing_cli import add_commands

    add_commands(commands)
    db = commands.add_parser("db", help="Apply versioned database migrations")
    db.add_argument("action", choices=["upgrade"])
    serve = commands.add_parser("serve", help="Run the read-only API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    ingest = commands.add_parser("ingest", help="Poll ETH/USD exchanges, oracles, and the DEX pool")
    ingest.add_argument("--source", choices=["all", *SOURCES], default="all")
    ingest.add_argument("--once", action="store_true")
    ingest.add_argument(
        "--transport", choices=["direct", "redis"], help="Override ingestion transport"
    )
    ingest.add_argument(
        "--coinbase-stream",
        action="store_true",
        help="Stream Coinbase matches with REST gap recovery",
    )
    consume = commands.add_parser("consume", help="Persist queued polling attempts from Redis")
    consume.add_argument(
        "--once", action="store_true", help="Process one bounded recovery/new batch"
    )
    consume.add_argument("--consumer", help="Unique worker name (generated if omitted)")
    replay_parser = commands.add_parser("replay", help="Replay a fixture into isolated storage")
    replay_parser.add_argument("scenario", type=Path)
    analysis = commands.add_parser("analyze", help="Persist an explainable statistical round")
    analysis.add_argument("--dataset", default="live")
    analysis.add_argument("--at", help="Timezone-aware cutoff, e.g. 2026-10-16T00:00:10Z")
    analysis.add_argument("--window", type=int, default=300, help="History window in seconds")
    scoring = commands.add_parser("score", help="Persist diagnostic source quality scores")
    scoring.add_argument("--registry-id", required=True, help="Retained dependency registry hash")
    scoring.add_argument("--dataset", default="live")
    scoring.add_argument("--at", help="Timezone-aware cutoff, floored to the policy sampling grid")
    scoring.add_argument("--policy", type=Path, help="Scoring policy JSON; defaults to scoring-v1")
    weighting = commands.add_parser("weight", help="Apply quarantine/recovery and dependency caps")
    weighting.add_argument("scoring_report_id", help="Retained scoring report to apply once")
    weighting.add_argument("--dataset", default="live")
    weighting.add_argument(
        "--policy", type=Path, help="Weighting policy JSON; fixed for this dataset"
    )
    aggregation = commands.add_parser("aggregate", help="Persist a weighted-median candidate")
    aggregation.add_argument("weighting_report_id", help="Retained weighting report to aggregate")
    aggregation.add_argument("--dataset", default="live")
    confidence = commands.add_parser("confidence", help="Persist quality and availability gates")
    confidence.add_argument("aggregation_report_id", help="Retained aggregation to assess")
    confidence.add_argument("--dataset", default="live")
    confidence.add_argument("--policy", type=Path, help="Confidence policy JSON")
    benchmark = commands.add_parser(
        "benchmark", help="Evaluate a frozen full-pipeline synthetic suite"
    )
    benchmark.add_argument("suite", type=Path)
    benchmark.add_argument("--output", type=Path, required=True)
    benchmark.add_argument("--require-targets", action="store_true")
    demo = commands.add_parser("demo", help="Retain isolated synthetic dashboard replay rounds")
    demo.add_argument("suite", type=Path)
    demo.add_argument(
        "--case",
        default="all",
        choices=[
            "all",
            "baseline",
            "single_spike",
            "market_move",
            "correlated_pair",
            "correlated_majority",
            "source_outage",
            "shared_outage",
            "late_receipt",
        ],
    )
    evidence = commands.add_parser("evidence", help="Export or verify retained round commitments")
    evidence_commands = evidence.add_subparsers(dest="evidence_action", required=True)
    export = evidence_commands.add_parser("export")
    export.add_argument("assessment_id")
    export.add_argument("--dataset", default="live")
    export.add_argument("--output", type=Path, required=True)
    verify = evidence_commands.add_parser("verify")
    verify.add_argument("bundle", type=Path)
    proof = evidence_commands.add_parser("proof")
    proof.add_argument("bundle", type=Path)
    proof.add_argument("--index", type=int, required=True)
    proof.add_argument("--output", type=Path, required=True)
    verify_proof = evidence_commands.add_parser("verify-proof")
    verify_proof.add_argument("proof", type=Path)
    verify_proof.add_argument("--root", required=True, help="Independently obtained evidence root")
    dependencies = commands.add_parser(
        "dependencies", help="Version and inspect dependency evidence"
    )
    dependency_commands = dependencies.add_subparsers(dest="dependency_action", required=True)
    register = dependency_commands.add_parser("register", help="Persist an immutable registry")
    register.add_argument("--file", type=Path, help="Registry JSON; defaults to bundled review")
    inspect = dependency_commands.add_parser(
        "inspect", help="Inspect a retained registry by its hash"
    )
    inspect.add_argument("registry_id")
    inspect.add_argument("--source", choices=SOURCES, action="append", dest="sources")
    history = commands.add_parser("history", help="Capture and evaluate frozen historical suites")
    history_commands = history.add_subparsers(dest="history_action", required=True)
    for action in ("fetch", "evaluate"):
        historical = history_commands.add_parser(action)
        historical.add_argument("suite", type=Path)
        historical.add_argument("captures", type=Path)
        if action == "evaluate":
            historical.add_argument("--output", type=Path, required=True)
            historical.add_argument(
                "--require-targets",
                action="store_true",
                help="Exit unsuccessfully after writing results if held-out targets are unmet",
            )
    args = parser.parse_args()
    if args.command == "signing":
        from verdict.signing_cli import run

        run(args, parser)
        return
    if args.command == "evidence" and args.evidence_action != "export":
        from verdict.evidence import (
            EvidenceBundle,
            InclusionProof,
            inclusion_proof,
            verify_bundle,
            verify_inclusion,
        )

        if args.evidence_action == "verify-proof":
            proof = InclusionProof.model_validate_json(args.proof.read_text(encoding="utf-8"))
            if not verify_inclusion(proof, expected_root=args.root):
                parser.error("evidence inclusion proof does not match the supplied root")
            print(json.dumps({"verified": True, "root": args.root}))
        else:
            bundle = verify_bundle(
                EvidenceBundle.model_validate_json(args.bundle.read_text(encoding="utf-8"))
            )
            if args.evidence_action == "proof":
                result = inclusion_proof(bundle, args.index)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as output:
                    output.write(result.model_dump_json(indent=2) + "\n")
            print(json.dumps({"verified": True, "root": bundle.root, "leaves": len(bundle.leaves)}))
        return
    if args.command == "benchmark":
        from verdict.adversarial import evaluate_adversarial

        report = evaluate_adversarial(args.suite)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "report_id": report["report_id"],
                    "output": str(args.output),
                    "attack_targets_met": report["attack_targets_met"],
                }
            )
        )
        if args.require_targets and not report["attack_targets_met"]:
            raise SystemExit(1)
        return
    if args.command == "history":
        if args.history_action == "fetch":
            from verdict.historical import fetch_history

            asyncio.run(fetch_history(args.suite, args.captures))
        else:
            from verdict.benchmark import evaluate_history

            report = evaluate_history(args.suite, args.captures)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"report_id": report["report_id"], "output": str(args.output)}))
            if args.require_targets and not report["heldout_targets_met"]:
                raise SystemExit(1)
        return
    if args.command == "ingest" and args.coinbase_stream:
        if args.once or args.source not in ("all", "coinbase"):
            parser.error(
                "--coinbase-stream requires continuous ingestion with --source all or coinbase"
            )
    settings = get_settings()
    if args.command == "ingest" and args.coinbase_stream:
        settings = settings.model_copy(update={"coinbase_stream_enabled": True})
    if args.command == "ingest" and args.transport:
        settings = settings.model_copy(update={"ingestion_transport": args.transport})
    if (
        args.command == "ingest"
        and settings.ingestion_transport == "redis"
        and settings.coinbase_stream_enabled
        and not args.once
        and args.source in ("all", "coinbase")
    ):
        parser.error("Redis transport supports polling; disable Coinbase streaming or use direct")
    if (
        args.command == "consume"
        and args.consumer is not None
        and not 1 <= len(args.consumer) <= 128
    ):
        parser.error("--consumer must contain 1..128 characters")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "serve":
        uvicorn.run("verdict.api:create_app", factory=True, host=args.host, port=args.port)
        return
    if args.command == "db":
        command.upgrade(Config("alembic.ini"), "head")
        return
    queued_producer = args.command == "ingest" and settings.ingestion_transport == "redis"
    engine = None if queued_producer else build_engine(settings.database_url)
    try:
        store = ObservationStore(engine) if engine is not None else None
        if store is not None:
            store.check_ready()
        if args.command == "evidence":
            from verdict.evidence import build_bundle

            bundle = build_bundle(store, dataset=args.dataset, assessment_id=args.assessment_id)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(bundle.model_dump_json(indent=2) + "\n")
            print(json.dumps({"root": bundle.root, "leaves": len(bundle.leaves)}))
            return
        if args.command == "demo":
            from verdict.dashboard import seed_demo

            print(json.dumps(seed_demo(store, args.suite, case=args.case)))
            return
        if args.command == "ingest":
            if queued_producer:
                from verdict.queue import run_queued_ingestion

                results = asyncio.run(
                    run_queued_ingestion(store, settings, source=args.source, once=args.once)
                )
                if args.once:
                    print(json.dumps(results, indent=2))
                    entries = results if isinstance(results, list) else [results]
                    if any(result["status"] == "error" for result in entries):
                        raise SystemExit(1)
            elif args.source == "all":
                results = asyncio.run(ingest_all(store, settings, once=args.once))
                if args.once:
                    print(json.dumps(results, indent=2))
                    if any(result["status"] == "error" for result in results):
                        raise SystemExit(1)
            else:
                result = asyncio.run(ingest_source(args.source, store, settings, once=args.once))
                if args.once:
                    print(json.dumps(result, indent=2))
        elif args.command == "consume":
            from verdict.queue import consume_queue

            counts = asyncio.run(
                consume_queue(store, settings, consumer=args.consumer, once=args.once)
            )
            if args.once:
                print(json.dumps(counts, indent=2))
                if counts.get("rejected"):
                    raise SystemExit(1)
        elif args.command == "replay":
            print(json.dumps(replay(args.scenario, store), indent=2))
        elif args.command == "dependencies":
            if args.dependency_action == "register":
                registry = (
                    DependencyRegistry.model_validate_json(args.file.read_text(encoding="utf-8"))
                    if args.file
                    else bundled_registry()
                )
                inserted = store.save_dependency_registry(registry)
                print(
                    json.dumps(
                        {
                            "registry_id": registry.registry_id,
                            "version": registry.version,
                            "inserted": inserted,
                        }
                    )
                )
            else:
                registry = store.get_dependency_registry(args.registry_id)
                if registry is None:
                    parser.error(
                        "dependency registry not found; register the selected version first"
                    )
                try:
                    report = inspect_dependencies(registry, tuple(args.sources or SOURCES))
                except ValueError as error:
                    parser.error(str(error))
                print(json.dumps(report, indent=2))
        elif args.command == "confidence":
            try:
                dataset = TypeAdapter(Dataset).validate_python(args.dataset)
                policy = (
                    ConfidencePolicy.model_validate_json(args.policy.read_text(encoding="utf-8"))
                    if args.policy
                    else ConfidencePolicy()
                )
                report = build_confidence_report(
                    store,
                    aggregation_report_id=args.aggregation_report_id,
                    dataset=dataset,
                    policy=policy,
                )
                store.save_confidence_round(report)
                print(report.model_dump_json(indent=2))
            except ValueError as error:
                parser.error(str(error))
        elif args.command == "aggregate":
            try:
                dataset = TypeAdapter(Dataset).validate_python(args.dataset)
                report = build_aggregation_report(
                    store, weighting_report_id=args.weighting_report_id, dataset=dataset
                )
                store.save_aggregation_round(report)
                print(report.model_dump_json(indent=2))
            except ValueError as error:
                parser.error(str(error))
        elif args.command == "weight":
            try:
                dataset = TypeAdapter(Dataset).validate_python(args.dataset)
                policy = (
                    WeightingPolicy.model_validate_json(args.policy.read_text(encoding="utf-8"))
                    if args.policy
                    else WeightingPolicy()
                )
                report = store.apply_weighting(
                    args.scoring_report_id, dataset=dataset, policy=policy
                )
                print(report.model_dump_json(indent=2))
            except ValueError as error:
                parser.error(str(error))
        elif args.command == "score":
            try:
                dataset = TypeAdapter(Dataset).validate_python(args.dataset)
                if dataset.startswith("replay:") and args.at is None:
                    parser.error("replay scoring requires --at for a reproducible cutoff")
                policy = (
                    ScoringPolicy.model_validate_json(args.policy.read_text(encoding="utf-8"))
                    if args.policy
                    else ScoringPolicy()
                )
                cutoff = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
                report = build_scoring_report(
                    store,
                    dataset=dataset,
                    cutoff=cutoff,
                    registry_id=args.registry_id,
                    policy=policy,
                )
                store.save_scoring_round(report)
                print(report.model_dump_json(indent=2))
            except ValueError as error:
                parser.error(str(error))
        elif args.command == "analyze":
            dataset = TypeAdapter(Dataset).validate_python(args.dataset)
            if dataset.startswith("replay:") and args.at is None:
                parser.error("replay analysis requires --at for a reproducible cutoff")
            cutoff = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
            report = build_report(
                store,
                dataset=dataset,
                cutoff=cutoff,
                policy=AnalysisPolicy(
                    history_seconds=args.window,
                    freshness_seconds=settings.freshness_seconds,
                    max_future_skew_seconds=settings.max_future_skew_seconds,
                ),
            )
            store.save_round(report)
            print(report.model_dump_json(indent=2))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("ingestion_stopped")
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()
