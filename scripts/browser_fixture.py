"""Seed and serve an isolated browser-test database, or append one real round event."""

import argparse
import json
import os
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from verdict.api import create_app
from verdict.confidence import ConfidencePolicy, build_confidence_report
from verdict.config import Settings
from verdict.dashboard import seed_demo
from verdict.storage import ObservationStore, build_engine

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "append"))
    args = parser.parse_args()
    database = Path(os.environ["VERDICT_E2E_DATABASE"]).resolve()
    cache = (ROOT / ".cache" / "browser-tests").resolve()
    if database.parent != cache or database.suffix != ".db":
        parser.error("browser tests require a database directly under .cache/browser-tests")
    database.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{database.as_posix()}"
    settings = Settings(
        _env_file=None,
        database_url=url,
        dashboard_origins=("http://127.0.0.1:3011",),
    )
    if args.action == "serve":
        if database.exists():
            parser.error("refusing to reuse an existing browser-test database")
        os.environ["VERDICT_DATABASE_URL"] = url
        migration = Config(str(ROOT / "alembic.ini"))
        command.upgrade(migration, "head")
    engine = build_engine(url)
    store = ObservationStore(engine)
    try:
        if args.action == "append":
            dataset = "replay:adv-synthetic-baseline"
            latest = store.confidence_rounds(dataset=dataset, limit=1)[0]
            # Different immutable policy identity, same retained evidence and cutoff.
            report = build_confidence_report(
                store,
                dataset=dataset,
                aggregation_report_id=latest["aggregation_report_id"],
                policy=ConfidencePolicy(minimum_quality_bps=8001),
            )
            inserted = store.save_confidence_round(report)
            print(json.dumps({"report_id": report.report_id, "inserted": inserted}))
        else:
            for case in ("baseline", "single_spike", "source_outage"):
                seed_demo(store, ROOT / "benchmarks/adversarial-v1.json", case=case)
            uvicorn.run(create_app(settings, store), host="127.0.0.1", port=8011)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
