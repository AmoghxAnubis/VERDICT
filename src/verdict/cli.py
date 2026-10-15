import argparse
import asyncio
import json
import logging
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from verdict.config import get_settings
from verdict.ingestion import ingest_coinbase
from verdict.replay import replay
from verdict.storage import ObservationStore, build_engine


def main() -> None:
    parser = argparse.ArgumentParser(prog="verdict")
    commands = parser.add_subparsers(dest="command", required=True)
    db = commands.add_parser("db", help="Apply versioned database migrations")
    db.add_argument("action", choices=["upgrade"])
    serve = commands.add_parser("serve", help="Run the read-only API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    ingest = commands.add_parser("ingest", help="Poll public Coinbase ETH-USD market data")
    ingest.add_argument("--once", action="store_true")
    replay_parser = commands.add_parser("replay", help="Replay a fixture into isolated storage")
    replay_parser.add_argument("scenario", type=Path)
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
            asyncio.run(ingest_coinbase(store, settings, once=args.once))
        elif args.command == "replay":
            print(json.dumps(replay(args.scenario, store), indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
