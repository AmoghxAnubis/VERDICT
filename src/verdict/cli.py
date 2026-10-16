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

from verdict.config import get_settings
from verdict.ingestion import ingest_all, ingest_source
from verdict.models import SOURCES, Dataset
from verdict.replay import replay
from verdict.statistics import AnalysisPolicy, build_report
from verdict.storage import ObservationStore, build_engine


def main() -> None:
    parser = argparse.ArgumentParser(prog="verdict")
    commands = parser.add_subparsers(dest="command", required=True)
    db = commands.add_parser("db", help="Apply versioned database migrations")
    db.add_argument("action", choices=["upgrade"])
    serve = commands.add_parser("serve", help="Run the read-only API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    ingest = commands.add_parser("ingest", help="Poll public ETH/USD exchange trades")
    ingest.add_argument("--source", choices=["all", *SOURCES], default="all")
    ingest.add_argument("--once", action="store_true")
    replay_parser = commands.add_parser("replay", help="Replay a fixture into isolated storage")
    replay_parser.add_argument("scenario", type=Path)
    analysis = commands.add_parser("analyze", help="Persist an explainable statistical round")
    analysis.add_argument("--dataset", default="live")
    analysis.add_argument("--at", help="Timezone-aware cutoff, e.g. 2026-10-16T00:00:10Z")
    analysis.add_argument("--window", type=int, default=300, help="History window in seconds")
    args = parser.parse_args()
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "serve":
        uvicorn.run("verdict.api:create_app", factory=True, host=args.host, port=args.port)
        return
    if args.command == "db":
        command.upgrade(Config("alembic.ini"), "head")
        return
    engine = build_engine(settings.database_url)
    try:
        store = ObservationStore(engine)
        store.check_ready()
        if args.command == "ingest":
            if args.source == "all":
                results = asyncio.run(ingest_all(store, settings, once=args.once))
                if args.once:
                    print(json.dumps(results, indent=2))
                    if any(result["status"] == "error" for result in results):
                        raise SystemExit(1)
            else:
                result = asyncio.run(ingest_source(args.source, store, settings, once=args.once))
                if args.once:
                    print(json.dumps(result, indent=2))
        elif args.command == "replay":
            print(json.dumps(replay(args.scenario, store), indent=2))
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
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
