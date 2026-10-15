import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from verdict import __version__
from verdict.config import Settings, get_settings
from verdict.models import Asset, Dataset, Observation
from verdict.storage import ObservationStore, build_engine

logger = logging.getLogger(__name__)


class ObservationResponse(Observation):
    observation_id: str


def create_app(settings: Settings | None = None, store: ObservationStore | None = None) -> FastAPI:
    settings = settings or get_settings()
    owns_engine = store is None
    store = store or ObservationStore(build_engine(settings.database_url))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if owns_engine:
            store.engine.dispose()

    app = FastAPI(title="VERDICT API", version=__version__, lifespan=lifespan)

    @app.get("/health/live")
    def liveness() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready")
    def readiness() -> dict:
        try:
            store.check_ready()
        except SQLAlchemyError as error:
            logger.warning("storage_not_ready error_type=%s", type(error).__name__)
            raise HTTPException(
                503, detail="storage not ready; apply database migrations"
            ) from None
        return {"status": "ready"}

    @app.get("/v1/observations", response_model=list[ObservationResponse])
    def observations(
        dataset: Dataset = "live",
        asset: Asset = "ETH-USD",
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[dict]:
        return [
            {**observation.model_dump(), "observation_id": observation.observation_id}
            for observation in store.list(dataset=dataset, asset=asset, limit=limit)
        ]

    @app.get("/v1/sources")
    def sources(dataset: Dataset = "live") -> list[dict]:
        observations = store.list(dataset=dataset, limit=1)
        latest = observations[0] if observations else None
        now = datetime.now(UTC)
        age = (now - latest.timestamp).total_seconds() if latest else None
        state = "unavailable"
        if latest and age is not None:
            if age < -settings.max_future_skew_seconds:
                state = "clock_skew"
            else:
                state = "fresh" if age <= settings.freshness_seconds else "stale"
        return [
            {
                "source": "coinbase",
                "asset": "ETH-USD",
                "dataset": dataset,
                "observation_status": state,
                "age_seconds": age,
                "latest_timestamp": latest.timestamp if latest else None,
                "trust_score": None,
            }
        ]

    @app.get("/v1/feed/{asset}")
    def feed(asset: Asset) -> dict:
        raise HTTPException(
            503,
            detail={
                "asset": asset,
                "status": "unavailable",
                "value": None,
                "confidence": None,
                "reason": "consensus_engine_not_implemented",
            },
        )

    return app
