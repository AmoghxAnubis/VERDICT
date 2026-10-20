import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy.exc import SQLAlchemyError

from verdict import __version__
from verdict.aggregation import AggregationReport
from verdict.config import Settings, get_settings
from verdict.dependencies import Sha256, inspect_dependencies
from verdict.models import SOURCES, Asset, Dataset, ObservationData, Source
from verdict.scoring import ScoringReport
from verdict.statistics import AnalysisPolicy, AnalysisReport, build_report
from verdict.storage import ObservationStore, build_engine
from verdict.weighting import WeightingReport

logger = logging.getLogger(__name__)


class ObservationResponse(ObservationData):
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

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
        logger.warning("storage_unavailable error_type=%s", type(error).__name__)
        return JSONResponse(status_code=503, content={"detail": "storage unavailable"})

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
        source: Source | None = None,
    ) -> list[dict]:
        return [
            {**observation.model_dump(), "observation_id": observation.observation_id}
            for observation in store.list(dataset=dataset, asset=asset, limit=limit, source=source)
        ]

    @app.get("/v1/sources")
    def sources(dataset: Dataset = "live") -> list[dict]:
        now = datetime.now(UTC)
        health = store.health() if dataset == "live" else {}
        stream_states = store.stream_states() if dataset == "live" else {}
        results = []
        for source in SOURCES:
            observations = store.list(dataset=dataset, source=source, limit=1)
            latest = observations[0] if observations else None
            age = (now - latest.timestamp).total_seconds() if latest else None
            state = "unavailable"
            if age is not None:
                if age < -settings.max_future_skew_seconds:
                    state = "clock_skew"
                else:
                    state = "fresh" if age <= settings.freshness_seconds else "stale"
            results.append(
                {
                    "source": source,
                    "asset": "ETH-USD",
                    "dataset": dataset,
                    "observation_status": state,
                    "age_seconds": age,
                    "latest_timestamp": latest.timestamp if latest else None,
                    "trust_score": None,
                    "connector_health": health.get(source),
                    "stream": stream_states.get(source),
                }
            )
        return results

    @app.get("/v1/analysis/{asset}", response_model=AnalysisReport)
    def analysis(
        asset: Asset,
        dataset: Dataset = "live",
        at: AwareDatetime | None = None,
        window: Annotated[int, Query(ge=10, le=3600)] = 300,
    ) -> AnalysisReport:
        if dataset.startswith("replay:") and at is None:
            raise HTTPException(422, detail="replay analysis requires an explicit at cutoff")
        try:
            return build_report(
                store,
                dataset=dataset,
                cutoff=at or datetime.now(UTC),
                policy=AnalysisPolicy(
                    history_seconds=window,
                    freshness_seconds=settings.freshness_seconds,
                    max_future_skew_seconds=settings.max_future_skew_seconds,
                ),
            )
        except ValueError as error:
            raise HTTPException(422, detail=str(error)) from None

    @app.get("/v1/rounds", response_model=list[AnalysisReport])
    def rounds(dataset: Dataset = "live", limit: Annotated[int, Query(ge=1, le=1000)] = 100):
        return store.rounds(dataset=dataset, limit=limit)

    @app.get("/v1/rounds/{round_id}", response_model=AnalysisReport)
    def round_detail(round_id: str, dataset: Dataset = "live"):
        report = store.get_round(round_id, dataset=dataset)
        if report is None:
            raise HTTPException(404, detail="round not found in selected dataset")
        return report

    @app.get("/v1/scores", response_model=list[ScoringReport])
    def scoring_rounds(dataset: Dataset = "live", limit: Annotated[int, Query(ge=1, le=100)] = 10):
        return store.scoring_rounds(dataset=dataset, limit=limit)

    @app.get("/v1/scores/{report_id}", response_model=ScoringReport)
    def scoring_detail(report_id: Sha256, dataset: Dataset = "live"):
        report = store.get_scoring_round(report_id, dataset=dataset)
        if report is None:
            raise HTTPException(404, detail="scoring round not found in selected dataset")
        return report

    @app.get("/v1/weights", response_model=list[WeightingReport])
    def weighting_rounds(
        dataset: Dataset = "live", limit: Annotated[int, Query(ge=1, le=100)] = 10
    ):
        return store.weighting_rounds(dataset=dataset, limit=limit)

    @app.get("/v1/weights/{report_id}", response_model=WeightingReport)
    def weighting_detail(report_id: Sha256, dataset: Dataset = "live"):
        report = store.get_weighting_round(report_id, dataset=dataset)
        if report is None:
            raise HTTPException(404, detail="weighting round not found in selected dataset")
        return report

    @app.get("/v1/aggregations", response_model=list[AggregationReport])
    def aggregation_rounds(
        dataset: Dataset = "live", limit: Annotated[int, Query(ge=1, le=100)] = 10
    ):
        return store.aggregation_rounds(dataset=dataset, limit=limit)

    @app.get("/v1/aggregations/{report_id}", response_model=AggregationReport)
    def aggregation_detail(report_id: Sha256, dataset: Dataset = "live"):
        report = store.get_aggregation_round(report_id, dataset=dataset)
        if report is None:
            raise HTTPException(404, detail="aggregation round not found in selected dataset")
        return report

    @app.get("/v1/dependencies")
    def dependency_registries(limit: Annotated[int, Query(ge=1, le=1000)] = 100):
        return store.dependency_registries(limit=limit)

    def retained_registry(registry_id: str):
        registry = store.get_dependency_registry(registry_id)
        if registry is None:
            raise HTTPException(404, detail="dependency registry not found")
        return registry

    @app.get("/v1/dependencies/{registry_id}")
    def dependency_registry(registry_id: Sha256):
        return retained_registry(registry_id).envelope()

    @app.get("/v1/dependencies/{registry_id}/inspection")
    def dependency_inspection(
        registry_id: Sha256, source: Annotated[list[Source] | None, Query()] = None
    ):
        registry = retained_registry(registry_id)
        try:
            return inspect_dependencies(registry, tuple(source or SOURCES))
        except ValueError as error:
            raise HTTPException(422, detail=str(error)) from None

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
