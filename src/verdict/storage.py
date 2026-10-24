import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, localcontext
from itertools import pairwise
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from verdict.aggregation import AggregationReport, aggregate
from verdict.confidence import ConfidenceReport, assess_confidence
from verdict.dependencies import DependencyRegistry, canonical_json, checksum
from verdict.event_identity import same_event_content
from verdict.models import Observation, Source
from verdict.queue_models import QueuedAttempt
from verdict.scoring import ScoringReport, availability
from verdict.weighting import WeightingPolicy, WeightingReport, advance_weights


class Base(DeclarativeBase):
    pass


class ObservationRow(Base):
    __tablename__ = "observations"
    __table_args__ = (
        Index(
            "ix_observation_lookup",
            "dataset",
            "asset",
            "source",
            "timestamp",
            "event_sequence",
            "id",
        ),
        Index("ix_observation_cutoff", "dataset", "asset", "received_at", "timestamp"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(64))
    asset: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    payload: Mapped[str] = mapped_column(Text)


class ConnectorHealthRow(Base):
    __tablename__ = "connector_health"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_attempt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)
    received: Mapped[int] = mapped_column(Integer)
    inserted: Mapped[int] = mapped_column(Integer)
    errors: Mapped[int] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer)
    last_error_type: Mapped[str | None] = mapped_column(String(128))
    last_http_status: Mapped[int | None] = mapped_column(Integer)


class AnalysisRoundRow(Base):
    __tablename__ = "analysis_rounds"
    __table_args__ = (Index("ix_round_lookup", "dataset", "asset", "cutoff", "id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(80))
    asset: Mapped[str] = mapped_column(String(16))
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[str] = mapped_column(Text)


class StreamCheckpointRow(Base):
    __tablename__ = "stream_checkpoints"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_trade_id: Mapped[int | None] = mapped_column(BigInteger)
    baseline_trade_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestionReceiptRow(Base):
    __tablename__ = "ingestion_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DependencyRegistryRow(Base):
    __tablename__ = "dependency_registries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[str] = mapped_column(Text)


class ScoringRoundRow(Base):
    __tablename__ = "scoring_rounds"
    __table_args__ = (Index("ix_scoring_lookup", "dataset", "cutoff", "id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(80))
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    registry_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)


class WeightingRoundRow(Base):
    __tablename__ = "weighting_rounds"
    __table_args__ = (
        Index("ix_weighting_lookup", "dataset", "cutoff", "id"),
        UniqueConstraint("dataset", "cutoff", name="uq_weighting_cutoff"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(80))
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scoring_report_id: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[str] = mapped_column(Text)


class WeightingHeadRow(Base):
    __tablename__ = "weighting_heads"

    dataset: Mapped[str] = mapped_column(String(80), primary_key=True)
    report_id: Mapped[str | None] = mapped_column(String(64))


class AggregationRoundRow(Base):
    __tablename__ = "aggregation_rounds"
    __table_args__ = (Index("ix_aggregation_lookup", "dataset", "cutoff", "id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(80))
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    weighting_report_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)


class ConfidenceRoundRow(Base):
    __tablename__ = "confidence_rounds"
    __table_args__ = (Index("ix_confidence_lookup", "dataset", "cutoff", "id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(80))
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    aggregation_report_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)


class AttemptConflict(ValueError):
    """An ingestion attempt identity was reused with different content."""


class RoundEventHeadRow(Base):
    __tablename__ = "round_event_head"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger)


class RoundEventRow(Base):
    __tablename__ = "round_events"
    __table_args__ = (Index("ix_round_event_dataset", "dataset", "sequence"),)

    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    dataset: Mapped[str] = mapped_column(String(80))
    assessment_id: Mapped[str] = mapped_column(String(64), unique=True)


class CheckpointConflict(ValueError):
    """Another worker advanced this checkpoint; reload before attempting recovery."""


class ObservationConflict(ValueError):
    """A provider reused an event identity with a different price or event time."""


def build_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    connect_args = (
        {"check_same_thread": False, "timeout": 30, "autocommit": False}
        if url.get_backend_name() == "sqlite"
        else {}
    )
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


class ObservationStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def session(self) -> Iterator[Session]:
        with Session(self.engine) as session:
            yield session

    def check_ready(self) -> None:
        with self.session() as session:
            session.execute(text("SELECT id FROM observations LIMIT 1"))
            session.execute(text("SELECT received_at FROM observations LIMIT 1"))
            session.execute(text("SELECT event_sequence FROM observations LIMIT 1"))
            session.execute(text("SELECT source FROM connector_health LIMIT 1"))
            session.execute(text("SELECT id FROM analysis_rounds LIMIT 1"))
            session.execute(text("SELECT last_trade_id FROM stream_checkpoints LIMIT 1"))
            session.execute(text("SELECT id FROM ingestion_receipts LIMIT 1"))
            session.execute(text("SELECT id FROM dependency_registries LIMIT 1"))
            session.execute(text("SELECT id FROM scoring_rounds LIMIT 1"))
            session.execute(text("SELECT id FROM weighting_rounds LIMIT 1"))
            session.execute(text("SELECT report_id FROM weighting_heads LIMIT 1"))
            session.execute(text("SELECT id FROM aggregation_rounds LIMIT 1"))
            session.execute(text("SELECT id FROM confidence_rounds LIMIT 1"))
            session.execute(text("SELECT sequence FROM round_events LIMIT 1"))
            if session.scalar(select(RoundEventHeadRow.sequence).where(RoundEventHeadRow.id == 1)) is None:
                raise ValueError("round event head is missing; apply database migrations")

    def save_confidence_round(self, report: ConfidenceReport) -> bool:
        report = ConfidenceReport.model_validate(report)
        payload = canonical_json(report.model_dump(mode="json"))
        with self.session() as session, session.begin():
            try:
                with session.begin_nested():
                    session.add(
                        ConfidenceRoundRow(
                            id=report.report_id,
                            dataset=report.dataset,
                            cutoff=report.cutoff,
                            aggregation_report_id=report.aggregation_report_id,
                            payload=payload,
                        )
                    )
                    session.flush()
            except IntegrityError:
                existing = session.scalar(
                    select(ConfidenceRoundRow.payload).where(
                        ConfidenceRoundRow.id == report.report_id
                    )
                )
                if existing != payload:
                    raise ValueError("immutable confidence round identity conflict") from None
                return False
            aggregation_row = session.get(AggregationRoundRow, report.aggregation_report_id)
            if aggregation_row is None:
                raise ValueError("confidence aggregation report must be retained")
            aggregation = AggregationReport.model_validate(
                self._aggregation_payload(aggregation_row)
            )
            score, registry = self._validate_aggregation_evidence(session, aggregation)
            if assess_confidence(aggregation, score, registry, policy=report.policy) != report:
                raise ValueError("confidence report does not reproduce from retained evidence")
            # A transactional row update serializes allocation until commit. A normal
            # PostgreSQL sequence could expose N+1 before the transaction for N commits.
            sequence = session.scalar(
                update(RoundEventHeadRow)
                .where(RoundEventHeadRow.id == 1)
                .values(sequence=RoundEventHeadRow.sequence + 1)
                .returning(RoundEventHeadRow.sequence)
            )
            if sequence is None:
                raise ValueError("round event head is missing")
            session.add(RoundEventRow(sequence=sequence, dataset=report.dataset,
                                      assessment_id=report.report_id))
        return True

    def round_events(self, *, dataset: str, after: int, limit: int = 100) -> dict:
        with self.session() as session:
            head = session.scalar(select(RoundEventHeadRow.sequence).where(RoundEventHeadRow.id == 1))
            if head is None or after < 0 or after > head:
                raise ValueError("invalid event cursor; reload the dashboard snapshot")
            rows = list(session.scalars(
                select(RoundEventRow).where(RoundEventRow.dataset == dataset,
                                           RoundEventRow.sequence > after)
                .order_by(RoundEventRow.sequence).limit(limit + 1)
            ))
            events = [{"cursor": row.sequence, "assessment_id": row.assessment_id,
                       "dataset": row.dataset, "type": "round"} for row in rows[:limit]]
            # Advance through other datasets only once the selected backlog is exhausted.
            cursor = events[-1]["cursor"] if len(rows) > limit else head
            return {"events": events, "cursor": cursor, "has_more": len(rows) > limit}

    def datasets(self) -> list[str]:
        with self.session() as session:
            datasets = set(session.scalars(select(ObservationRow.dataset).distinct()))
            datasets.update(session.scalars(select(ConfidenceRoundRow.dataset).distinct()))
            return ["live", *sorted(datasets - {"live"})]

    @staticmethod
    def _confidence_payload(row: ConfidenceRoundRow) -> dict:
        report = ConfidenceReport.model_validate_json(row.payload)
        if (report.report_id, report.dataset, report.cutoff, report.aggregation_report_id) != (
            row.id,
            row.dataset,
            row.cutoff.replace(tzinfo=UTC) if row.cutoff.tzinfo is None else row.cutoff,
            row.aggregation_report_id,
        ):
            raise ValueError("stored confidence round metadata mismatch")
        return report.model_dump(mode="json")

    def confidence_rounds(self, *, dataset: str = "live", limit: int = 10) -> list[dict]:
        with self.session() as session:
            rows = session.scalars(
                select(ConfidenceRoundRow)
                .where(ConfidenceRoundRow.dataset == dataset)
                .order_by(ConfidenceRoundRow.cutoff.desc(), ConfidenceRoundRow.id.desc())
                .limit(limit)
            )
            return [self._confidence_payload(row) for row in rows]

    def get_confidence_round(self, report_id: str, *, dataset: str = "live") -> dict | None:
        with self.session() as session:
            row = session.scalar(
                select(ConfidenceRoundRow).where(
                    ConfidenceRoundRow.id == report_id, ConfidenceRoundRow.dataset == dataset
                )
            )
            return self._confidence_payload(row) if row is not None else None

    def save_aggregation_round(self, report: AggregationReport) -> bool:
        report = AggregationReport.model_validate(report)
        payload = canonical_json(report.model_dump(mode="json"))
        with self.session() as session, session.begin():
            try:
                with session.begin_nested():
                    session.add(
                        AggregationRoundRow(
                            id=report.report_id,
                            dataset=report.dataset,
                            cutoff=report.cutoff,
                            weighting_report_id=report.weighting_report_id,
                            payload=payload,
                        )
                    )
                    session.flush()
            except IntegrityError:
                existing = session.scalar(
                    select(AggregationRoundRow.payload).where(
                        AggregationRoundRow.id == report.report_id
                    )
                )
                if existing != payload:
                    raise ValueError("immutable aggregation round identity conflict") from None
                return False
            self._validate_aggregation_evidence(session, report)
        return True

    def _validate_aggregation_evidence(
        self, session: Session, report: AggregationReport
    ) -> tuple[ScoringReport, DependencyRegistry]:
        # Called after the insert, within the same transaction. Confidence also
        # verifies original observations, even when aggregation was saved earlier.
        weights_row = session.get(WeightingRoundRow, report.weighting_report_id)
        score_row = session.get(ScoringRoundRow, report.scoring_report_id)
        if weights_row is None or score_row is None:
            raise ValueError("aggregation score and weights must be retained")
        weights = self._weighting_payload(weights_row)
        score = ScoringReport.model_validate(self._scoring_payload(score_row))
        if aggregate(score, weights) != report:
            raise ValueError("aggregation report does not reproduce from retained evidence")
        registry_row = session.get(DependencyRegistryRow, report.registry_id)
        if registry_row is None:
            raise ValueError("aggregation dependency registry is missing")
        registry = DependencyRegistry.model_validate_json(registry_row.payload)
        if registry.registry_id != report.registry_id:
            raise ValueError("aggregation dependency evidence mismatch")
        inputs = [
            Observation.model_validate_json(value)
            for value in session.scalars(
                select(ObservationRow.payload).where(
                    ObservationRow.dataset == report.dataset,
                    ObservationRow.id.in_(score.input_observation_ids),
                )
            )
        ]
        if (
            len(inputs) != len(score.input_observation_ids)
            or checksum({row.observation_id: row.model_dump(mode="json") for row in inputs})
            != report.input_sha256
        ):
            raise ValueError("aggregation input evidence is missing or changed")
        by_id = {row.observation_id: row for row in inputs}
        timing = {item.source: item for item in score.policy.source_timing}
        with localcontext() as context:
            context.prec = 60
            context.rounding = ROUND_HALF_EVEN
            for item in report.inputs:
                sample = item.sample
                row = by_id.get(sample.observation_id)
                if sample.observation_id is not None and (
                    row is None
                    or row.source != sample.source
                    or row.dataset != report.dataset
                    or row.timestamp > report.cutoff
                    or row.received_at > report.cutoff
                ):
                    raise ValueError("aggregation selected observation mismatch")
                expected = availability(row, report.cutoff, timing[sample.source])
                if any(getattr(sample, key) != value for key, value in expected.items()):
                    raise ValueError("aggregation selected observation mismatch")
        return score, registry

    @staticmethod
    def _aggregation_payload(row: AggregationRoundRow) -> dict:
        report = AggregationReport.model_validate_json(row.payload)
        if (report.report_id, report.dataset, report.cutoff, report.weighting_report_id) != (
            row.id,
            row.dataset,
            row.cutoff.replace(tzinfo=UTC) if row.cutoff.tzinfo is None else row.cutoff,
            row.weighting_report_id,
        ):
            raise ValueError("stored aggregation round metadata mismatch")
        return report.model_dump(mode="json")

    def aggregation_rounds(self, *, dataset: str = "live", limit: int = 10) -> list[dict]:
        with self.session() as session:
            rows = session.scalars(
                select(AggregationRoundRow)
                .where(AggregationRoundRow.dataset == dataset)
                .order_by(AggregationRoundRow.cutoff.desc(), AggregationRoundRow.id.desc())
                .limit(limit)
            )
            return [self._aggregation_payload(row) for row in rows]

    def get_aggregation_round(self, report_id: str, *, dataset: str = "live") -> dict | None:
        with self.session() as session:
            row = session.scalar(
                select(AggregationRoundRow).where(
                    AggregationRoundRow.id == report_id, AggregationRoundRow.dataset == dataset
                )
            )
            return self._aggregation_payload(row) if row is not None else None

    @staticmethod
    def _weighting_payload(row: WeightingRoundRow) -> WeightingReport:
        report = WeightingReport.model_validate_json(row.payload)
        if (report.report_id, report.dataset, report.cutoff, report.scoring_report_id) != (
            row.id,
            row.dataset,
            row.cutoff.replace(tzinfo=UTC) if row.cutoff.tzinfo is None else row.cutoff,
            row.scoring_report_id,
        ):
            raise ValueError("stored weighting round metadata mismatch")
        return report

    def apply_weighting(
        self,
        scoring_report_id: str,
        *,
        dataset: str = "live",
        policy: WeightingPolicy | None = None,
    ) -> WeightingReport:
        from sqlalchemy.dialects.postgresql import insert as postgres_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        policy = WeightingPolicy.model_validate(policy or WeightingPolicy())
        insert = sqlite_insert if self.engine.dialect.name == "sqlite" else postgres_insert
        with self.session() as session, session.begin():
            # First operation is a write: SQLite avoids read-lock upgrades and
            # PostgreSQL locks this dataset's head until the whole transition commits.
            session.execute(
                insert(WeightingHeadRow)
                .values(dataset=dataset, report_id=None)
                .on_conflict_do_update(index_elements=["dataset"], set_={"dataset": dataset})
            )
            head = session.get(WeightingHeadRow, dataset)
            latest = session.scalar(
                select(WeightingRoundRow)
                .where(WeightingRoundRow.dataset == dataset)
                .order_by(WeightingRoundRow.cutoff.desc(), WeightingRoundRow.id.desc())
                .limit(1)
            )
            # A missing/reset/older pointer must not silently start a second chain.
            if head.report_id != (latest.id if latest is not None else None):
                raise ValueError("weighting head does not match latest retained round")
            previous = self._weighting_payload(latest) if latest is not None else None
            existing = session.scalar(
                select(WeightingRoundRow).where(
                    WeightingRoundRow.scoring_report_id == scoring_report_id,
                    WeightingRoundRow.dataset == dataset,
                )
            )
            if existing is not None:
                report = self._weighting_payload(existing)
                if report.policy != policy:
                    raise ValueError(
                        "already applied scoring report with a different weighting policy"
                    )
                return report
            score_row = session.scalar(
                select(ScoringRoundRow).where(
                    ScoringRoundRow.id == scoring_report_id,
                    ScoringRoundRow.dataset == dataset,
                )
            )
            if score_row is None:
                raise ValueError("scoring report not found in selected dataset")
            score = ScoringReport.model_validate(self._scoring_payload(score_row))
            registry_row = session.get(DependencyRegistryRow, score.registry_id)
            if registry_row is None:
                raise ValueError("scoring dependency registry is missing")
            registry = DependencyRegistry.model_validate_json(registry_row.payload)
            report = advance_weights(score, registry, policy=policy, previous=previous)
            session.add(
                WeightingRoundRow(
                    id=report.report_id,
                    dataset=report.dataset,
                    cutoff=report.cutoff,
                    scoring_report_id=score.report_id,
                    payload=canonical_json(report.model_dump(mode="json")),
                )
            )
            head.report_id = report.report_id
        return report

    def weighting_rounds(self, *, dataset: str = "live", limit: int = 10) -> list[dict]:
        with self.session() as session:
            rows = session.scalars(
                select(WeightingRoundRow)
                .where(WeightingRoundRow.dataset == dataset)
                .order_by(WeightingRoundRow.cutoff.desc(), WeightingRoundRow.id.desc())
                .limit(limit)
            )
            return [self._weighting_payload(row).model_dump(mode="json") for row in rows]

    def get_weighting_round(self, report_id: str, *, dataset: str = "live") -> dict | None:
        with self.session() as session:
            row = session.scalar(
                select(WeightingRoundRow).where(
                    WeightingRoundRow.id == report_id,
                    WeightingRoundRow.dataset == dataset,
                )
            )
            return self._weighting_payload(row).model_dump(mode="json") if row else None

    def scoring_observations(
        self, *, dataset: str, first: datetime, cutoff: datetime, max_observations: int
    ) -> list[Observation]:
        # One pre-window anchor per provider, then every newly visible event. A late
        # receipt can matter even when its event time predates the first sample.
        ranked = (
            select(
                ObservationRow.id,
                func.row_number()
                .over(
                    partition_by=ObservationRow.source,
                    order_by=(
                        ObservationRow.timestamp.desc(),
                        ObservationRow.event_sequence.desc(),
                        ObservationRow.id.desc(),
                    ),
                )
                .label("rank"),
            )
            .where(
                ObservationRow.dataset == dataset,
                ObservationRow.timestamp <= first,
                ObservationRow.received_at <= first,
            )
            .subquery()
        )
        statement = (
            select(ObservationRow.payload)
            .where(
                ObservationRow.dataset == dataset,
                ObservationRow.timestamp <= cutoff,
                ObservationRow.received_at <= cutoff,
                or_(
                    ObservationRow.id.in_(select(ranked.c.id).where(ranked.c.rank == 1)),
                    ObservationRow.timestamp > first,
                    ObservationRow.received_at > first,
                ),
            )
            .limit(max_observations + 1)
        )
        with self.session() as session:
            payloads = list(session.scalars(statement))
        if len(payloads) > max_observations:
            raise ValueError("scoring observation limit exceeded; narrow the window")
        return [Observation.model_validate_json(payload) for payload in payloads]

    def save_scoring_round(self, report: ScoringReport) -> bool:
        report = ScoringReport.model_validate(report)
        payload = canonical_json(report.model_dump(mode="json"))
        with self.session() as session, session.begin():
            try:
                with session.begin_nested():
                    session.add(
                        ScoringRoundRow(
                            id=report.report_id,
                            dataset=report.dataset,
                            cutoff=report.cutoff,
                            registry_id=report.registry_id,
                            payload=payload,
                        )
                    )
                    session.flush()
            except IntegrityError:
                existing = session.scalar(
                    select(ScoringRoundRow.payload).where(ScoringRoundRow.id == report.report_id)
                )
                if existing != payload:
                    raise ValueError("immutable scoring round identity conflict") from None
                return False
            # Write first to avoid upgrading competing SQLite read transactions.
            # A missing registry rolls the entire insertion back before commit.
            registry_row = session.get(DependencyRegistryRow, report.registry_id)
            if registry_row is None:
                raise ValueError("dependency registry must be retained before scoring round")
            registry = DependencyRegistry.model_validate_json(registry_row.payload)
            if (
                registry.registry_id != report.registry_id
                or registry.reviewed_at != report.registry_reviewed_at
            ):
                raise ValueError("scoring dependency evidence mismatch")
            payloads = session.scalars(
                select(ObservationRow.payload).where(
                    ObservationRow.id.in_(report.input_observation_ids),
                    ObservationRow.dataset == report.dataset,
                )
            )
            inputs = [Observation.model_validate_json(value) for value in payloads]
            if (
                len(inputs) != len(report.input_observation_ids)
                or checksum({row.observation_id: row.model_dump(mode="json") for row in inputs})
                != report.input_sha256
            ):
                raise ValueError("scoring input evidence is missing or changed")
        return True

    @staticmethod
    def _scoring_payload(row: ScoringRoundRow) -> dict:
        report = ScoringReport.model_validate_json(row.payload)
        if (report.report_id, report.dataset, report.cutoff, report.registry_id) != (
            row.id,
            row.dataset,
            row.cutoff.replace(tzinfo=UTC) if row.cutoff.tzinfo is None else row.cutoff,
            row.registry_id,
        ):
            raise ValueError("stored scoring round metadata mismatch")
        return report.model_dump(mode="json")

    def scoring_rounds(self, *, dataset: str = "live", limit: int = 100) -> list[dict]:
        with self.session() as session:
            rows = session.scalars(
                select(ScoringRoundRow)
                .where(ScoringRoundRow.dataset == dataset)
                .order_by(ScoringRoundRow.cutoff.desc(), ScoringRoundRow.id.desc())
                .limit(limit)
            )
            return [self._scoring_payload(row) for row in rows]

    def get_scoring_round(self, report_id: str, *, dataset: str = "live") -> dict | None:
        with self.session() as session:
            row = session.scalar(
                select(ScoringRoundRow).where(
                    ScoringRoundRow.id == report_id, ScoringRoundRow.dataset == dataset
                )
            )
            return self._scoring_payload(row) if row is not None else None

    def save_dependency_registry(self, registry: DependencyRegistry) -> bool:
        registry = DependencyRegistry.model_validate(registry)
        payload = canonical_json(registry.model_dump(mode="json"))
        with self.session() as session, session.begin():
            try:
                with session.begin_nested():
                    session.add(
                        DependencyRegistryRow(
                            id=registry.registry_id,
                            version=registry.version,
                            reviewed_at=registry.reviewed_at,
                            payload=payload,
                        )
                    )
                    session.flush()
            except IntegrityError:
                existing = session.scalar(
                    select(DependencyRegistryRow.payload).where(
                        DependencyRegistryRow.id == registry.registry_id,
                        DependencyRegistryRow.version == registry.version,
                    )
                )
                if existing != payload:
                    raise ValueError("immutable dependency registry version conflict") from None
                return False
        return True

    def get_dependency_registry(self, registry_id: str) -> DependencyRegistry | None:
        with self.session() as session:
            payload = session.scalar(
                select(DependencyRegistryRow.payload).where(DependencyRegistryRow.id == registry_id)
            )
        if payload is None:
            return None
        registry = DependencyRegistry.model_validate_json(payload)
        if registry.registry_id != registry_id:
            raise ValueError("stored dependency registry checksum mismatch")
        return registry

    def dependency_registries(self, *, limit: int = 100) -> list[dict]:
        with self.session() as session:
            rows = session.execute(
                select(
                    DependencyRegistryRow.id,
                    DependencyRegistryRow.version,
                    DependencyRegistryRow.reviewed_at,
                )
                .order_by(DependencyRegistryRow.reviewed_at.desc(), DependencyRegistryRow.id.desc())
                .limit(limit)
            )
            return [
                {
                    "registry_id": row.id,
                    "version": row.version,
                    "reviewed_at": row.reviewed_at.replace(tzinfo=UTC)
                    if row.reviewed_at.tzinfo is None
                    else row.reviewed_at.astimezone(UTC),
                }
                for row in rows
            ]

    @staticmethod
    def _insert(session: Session, observation: Observation) -> bool:
        try:
            with session.begin_nested():
                session.add(
                    ObservationRow(
                        id=observation.observation_id,
                        dataset=observation.dataset,
                        source=observation.source,
                        asset=observation.asset,
                        timestamp=observation.timestamp,
                        received_at=observation.received_at,
                        event_sequence=observation.event_sequence,
                        payload=observation.model_dump_json(),
                    )
                )
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(ObservationRow.payload).where(
                    ObservationRow.id == observation.observation_id
                )
            )
            if existing is None:
                raise
            previous = Observation.model_validate_json(existing)
            if not same_event_content(previous, observation):
                raise ObservationConflict(
                    "provider event changed price, timestamp, or sequence"
                ) from None
            return False
        return True

    def add(self, observation: Observation) -> bool:
        """Persist the first receipt of an event. Repeated polling is idempotent."""
        return self.add_many([observation]) == 1

    def add_many(self, observations: Iterable[Observation]) -> int:
        """Atomically insert a batch, preserving first receipts and rejecting conflicts."""
        inserted = 0
        with self.session() as session:
            with session.begin():
                for observation in observations:
                    inserted += self._insert(session, observation)
        return inserted

    def record_attempt(
        self,
        source: Source,
        *,
        observation: Observation | None = None,
        error_type: str | None = None,
        http_status: int | None = None,
    ) -> bool:
        if observation is not None and (
            observation.source != source or observation.dataset != "live"
        ):
            raise ValueError("connector health only accepts matching live observations")
        if (observation is None) == (error_type is None):
            raise ValueError("an attempt must contain either an observation or an error")
        with self.session() as session, session.begin():
            inserted = self._insert(session, observation) if observation else False
            self._record_health(
                session,
                source,
                inserted=int(inserted),
                received=int(observation is not None),
                error_type=error_type,
                http_status=http_status,
            )
        return bool(inserted)

    def consume_attempt(self, attempt: QueuedAttempt) -> str:
        """Commit an inbox receipt, observation, and health exactly once per attempt."""
        from sqlalchemy.dialects.postgresql import insert as postgres_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        # Revalidate even callers that used model_copy/model_construct.
        attempt = QueuedAttempt.model_validate_json(attempt.model_dump_json())
        insert = sqlite_insert if self.engine.dialect.name == "sqlite" else postgres_insert
        identity = str(attempt.attempt_id)
        with self.session() as session, session.begin():
            claimed = session.scalar(
                insert(IngestionReceiptRow)
                .values(
                    id=identity,
                    content_hash=attempt.content_hash,
                    attempted_at=attempt.attempted_at,
                    processed_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(IngestionReceiptRow.id)
            )
            # psycopg can report rowcount=-1 for INSERT ... ON CONFLICT.
            # RETURNING distinguishes a claim from a duplicate on both backends.
            if claimed is None:
                previous = session.get(IngestionReceiptRow, identity)
                if previous.content_hash != attempt.content_hash:
                    raise AttemptConflict("ingestion attempt identity changed content")
                return "duplicate_attempt"
            inserted = self._insert(session, attempt.observation) if attempt.observation else False
            self._record_health(
                session,
                attempt.source,
                inserted=int(inserted),
                received=int(attempt.observation is not None),
                error_type=attempt.error_type,
                http_status=attempt.http_status,
            )
            if attempt.observation is None:
                return "recorded_error"
            return "inserted" if inserted else "duplicate_observation"

    def _record_health(
        self,
        session: Session,
        source: Source,
        *,
        inserted: int,
        received: int,
        error_type: str | None = None,
        http_status: int | None = None,
    ) -> None:
        now = datetime.now(UTC)
        success = error_type is None
        from sqlalchemy.dialects.postgresql import insert as postgres_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        insert = sqlite_insert if self.engine.dialect.name == "sqlite" else postgres_insert
        values = dict(
            source=source,
            asset="ETH-USD",
            last_attempt=now,
            last_success=now if success else None,
            last_failure=None if success else now,
            attempts=received if success else 1,
            received=received,
            inserted=inserted,
            errors=int(not success),
            consecutive_failures=0 if success else 1,
            last_error_type=error_type,
            last_http_status=http_status,
        )
        table = ConnectorHealthRow.__table__
        updates = dict(
            last_attempt=now,
            last_success=now if success else table.c.last_success,
            last_failure=table.c.last_failure if success else now,
            attempts=table.c.attempts + (received if success else 1),
            received=table.c.received + received,
            inserted=table.c.inserted + inserted,
            errors=table.c.errors + int(not success),
            consecutive_failures=0 if success else table.c.consecutive_failures + 1,
            last_error_type=error_type,
            last_http_status=http_status,
        )
        session.execute(
            insert(table)
            .values(**values)
            .on_conflict_do_update(index_elements=["source", "asset"], set_=updates)
        )

    def stream_states(self) -> dict[str, dict]:
        with self.session() as session:
            rows = session.execute(select(StreamCheckpointRow.__table__)).mappings()
            return {
                row["source"]: {
                    key: (
                        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
                    )
                    if isinstance(value, datetime)
                    else value
                    for key, value in row.items()
                }
                for row in rows
            }

    def set_coinbase_stream_status(
        self,
        status: str,
        *,
        heartbeat_at: datetime | None = None,
    ) -> None:
        if status not in ("connecting", "recovering", "streaming", "disconnected"):
            raise ValueError("invalid stream status")
        from sqlalchemy.dialects.postgresql import insert as postgres_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        insert = sqlite_insert if self.engine.dialect.name == "sqlite" else postgres_insert
        values = dict(
            source="coinbase", asset="ETH-USD", status=status, updated_at=datetime.now(UTC)
        )
        updates = dict(status=status, updated_at=values["updated_at"])
        if heartbeat_at is not None:
            if heartbeat_at.tzinfo is None:
                raise ValueError("heartbeat receipt must be timezone-aware")
            values["last_heartbeat"] = heartbeat_at
            updates["last_heartbeat"] = heartbeat_at
        with self.session() as session, session.begin():
            session.execute(
                insert(StreamCheckpointRow)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["source", "asset"],
                    set_=updates,
                )
            )

    def record_coinbase_stream_batch(
        self,
        observations: list[Observation],
        *,
        expected_trade_id: int | None,
    ) -> int:
        """Commit a contiguous range, its health counts, and checkpoint together."""
        if not observations:
            raise ValueError("stream batch cannot be empty")
        trade_ids = []
        for observation in observations:
            event = observation.provider_event_id
            if (
                observation.source != "coinbase"
                or observation.dataset != "live"
                or not event.isascii()
                or not event.isdigit()
                or int(event) > 2**63 - 1
            ):
                raise ValueError("stream checkpoints only accept live Coinbase trade IDs")
            trade_ids.append(int(event))
        if expected_trade_id is None:
            if len(trade_ids) != 1:
                raise ValueError("initial bootstrap must contain one baseline trade")
        elif trade_ids[0] != expected_trade_id + 1:
            raise ValueError("stream batch does not continue its checkpoint")
        if any(current != previous + 1 for previous, current in pairwise(trade_ids)):
            raise ValueError("stream batch has a missing, repeated, or unordered trade ID")
        with self.session() as session, session.begin():
            # A conditional update also serializes competing writers on PostgreSQL/SQLite.
            statement = (
                update(StreamCheckpointRow)
                .where(
                    StreamCheckpointRow.source == "coinbase",
                    StreamCheckpointRow.asset == "ETH-USD",
                    StreamCheckpointRow.last_trade_id == expected_trade_id,
                )
                .values(last_trade_id=trade_ids[-1], updated_at=datetime.now(UTC))
            )
            if expected_trade_id is None:
                statement = statement.values(baseline_trade_id=trade_ids[0])
            if session.execute(statement).rowcount != 1:
                raise CheckpointConflict("stream checkpoint changed or was not initialized")
            inserted = sum(self._insert(session, observation) for observation in observations)
            self._record_health(session, "coinbase", inserted=inserted, received=len(observations))
        return inserted

    def health(self) -> dict[str, dict]:
        with self.session() as session:
            rows = session.execute(select(ConnectorHealthRow.__table__)).mappings()
            return {
                row["source"]: {
                    key: (value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC))
                    if isinstance(value, datetime)
                    else value
                    for key, value in row.items()
                }
                for row in rows
            }

    def snapshot(
        self,
        *,
        dataset: str,
        cutoff: datetime,
        history_seconds: int,
        freshness_seconds: int,
        max_observations: int = 10000,
    ) -> list[Observation]:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        cutoff = cutoff.astimezone(UTC)
        ranked = (
            select(
                ObservationRow.payload,
                ObservationRow.timestamp,
                func.row_number()
                .over(
                    partition_by=ObservationRow.source,
                    order_by=(
                        ObservationRow.timestamp.desc(),
                        ObservationRow.event_sequence.desc(),
                        ObservationRow.id.desc(),
                    ),
                )
                .label("rank"),
            )
            .where(
                ObservationRow.dataset == dataset,
                ObservationRow.asset == "ETH-USD",
                ObservationRow.timestamp <= cutoff,
                ObservationRow.received_at <= cutoff,
            )
            .subquery()
        )
        start = cutoff - timedelta(seconds=history_seconds + freshness_seconds)
        statement = (
            select(ranked.c.payload)
            .where(or_(ranked.c.rank == 1, ranked.c.timestamp >= start))
            .limit(max_observations + 1)
        )
        with self.session() as session:
            payloads = list(session.scalars(statement))
        if len(payloads) > max_observations:
            raise ValueError("analysis observation limit exceeded; narrow the history window")
        return [Observation.model_validate_json(payload) for payload in payloads]

    def save_round(self, report) -> bool:
        payload = report.model_dump_json()
        with self.session() as session, session.begin():
            try:
                with session.begin_nested():
                    session.add(
                        AnalysisRoundRow(
                            id=report.round_id,
                            dataset=report.dataset,
                            asset=report.asset,
                            cutoff=report.cutoff,
                            payload=payload,
                        )
                    )
                    session.flush()
            except IntegrityError:
                existing = session.scalar(
                    select(AnalysisRoundRow.payload).where(AnalysisRoundRow.id == report.round_id)
                )
                if existing != payload:
                    raise ValueError("immutable round identity conflict") from None
                return False
        return True

    def rounds(self, *, dataset: str = "live", limit: int = 100) -> list[dict]:
        statement = (
            select(AnalysisRoundRow.payload)
            .where(AnalysisRoundRow.dataset == dataset)
            .order_by(AnalysisRoundRow.cutoff.desc(), AnalysisRoundRow.id.desc())
            .limit(limit)
        )
        with self.session() as session:
            return [json.loads(payload) for payload in session.scalars(statement)]

    def get_round(self, round_id: str, *, dataset: str = "live") -> dict | None:
        with self.session() as session:
            payload = session.scalar(
                select(AnalysisRoundRow.payload).where(
                    AnalysisRoundRow.id == round_id, AnalysisRoundRow.dataset == dataset
                )
            )
        return json.loads(payload) if payload else None

    def list(
        self,
        *,
        dataset: str = "live",
        asset: str = "ETH-USD",
        limit: int = 100,
        source: Source | None = None,
    ) -> list[Observation]:
        statement = (
            select(ObservationRow)
            .where(ObservationRow.dataset == dataset, ObservationRow.asset == asset)
            .order_by(
                ObservationRow.timestamp.desc(),
                ObservationRow.event_sequence.desc(),
                ObservationRow.id.desc(),
            )
            .limit(limit)
        )
        if source is not None:
            statement = statement.where(ObservationRow.source == source)
        with self.session() as session:
            return [
                Observation.model_validate_json(row.payload) for row in session.scalars(statement)
            ]
