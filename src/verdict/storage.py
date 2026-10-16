import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from verdict.models import Observation, Source


class Base(DeclarativeBase):
    pass


class ObservationRow(Base):
    __tablename__ = "observations"
    __table_args__ = (
        Index("ix_observation_lookup", "dataset", "asset", "source", "timestamp", "id"),
        Index("ix_observation_cutoff", "dataset", "asset", "received_at", "timestamp"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(64))
    asset: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
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
            session.execute(text("SELECT source FROM connector_health LIMIT 1"))
            session.execute(text("SELECT id FROM analysis_rounds LIMIT 1"))

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
            if (previous.value, previous.timestamp) != (observation.value, observation.timestamp):
                raise ObservationConflict("provider event changed price or timestamp") from None
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
        now = datetime.now(UTC)
        success = observation is not None
        from sqlalchemy.dialects.postgresql import insert as postgres_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        insert = sqlite_insert if self.engine.dialect.name == "sqlite" else postgres_insert
        with self.session() as session, session.begin():
            inserted = self._insert(session, observation) if observation else False
            values = dict(
                source=source,
                asset="ETH-USD",
                last_attempt=now,
                last_success=now if success else None,
                last_failure=None if success else now,
                attempts=1,
                received=int(success),
                inserted=int(inserted),
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
                attempts=table.c.attempts + 1,
                received=table.c.received + int(success),
                inserted=table.c.inserted + int(inserted),
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
        return bool(inserted)

    def health(self) -> dict[str, dict]:
        with self.session() as session:
            rows = session.execute(select(ConnectorHealthRow.__table__)).mappings()
            return {
                row["source"]: {
                    key: value.replace(tzinfo=UTC) if isinstance(value, datetime) else value
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
                    order_by=(ObservationRow.timestamp.desc(), ObservationRow.id.desc()),
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
            .order_by(ObservationRow.timestamp.desc(), ObservationRow.id.desc())
            .limit(limit)
        )
        if source is not None:
            statement = statement.where(ObservationRow.source == source)
        with self.session() as session:
            return [
                Observation.model_validate_json(row.payload) for row in session.scalars(statement)
            ]
