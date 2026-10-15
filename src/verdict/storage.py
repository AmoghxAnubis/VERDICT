from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import DateTime, Index, String, Text, create_engine, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from verdict.models import Observation


class Base(DeclarativeBase):
    pass


class ObservationRow(Base):
    __tablename__ = "observations"
    __table_args__ = (
        Index("ix_observation_lookup", "dataset", "asset", "source", "timestamp", "id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(64))
    asset: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[str] = mapped_column(Text)


class ObservationConflict(ValueError):
    """A provider reused an event identity with a different price or event time."""


def build_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False, "timeout": 30} if url.drivername == "sqlite" else {}
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

    def add(self, observation: Observation) -> bool:
        """Persist the first receipt of an event. Repeated polling is idempotent."""
        with self.session() as session:
            session.add(
                ObservationRow(
                    id=observation.observation_id,
                    dataset=observation.dataset,
                    source=observation.source,
                    asset=observation.asset,
                    timestamp=observation.timestamp,
                    payload=observation.model_dump_json(),
                )
            )
            try:
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                existing = session.get(ObservationRow, observation.observation_id)
                if existing is None:
                    raise
                previous = Observation.model_validate_json(existing.payload)
                if (previous.value, previous.timestamp) != (
                    observation.value,
                    observation.timestamp,
                ):
                    raise ObservationConflict("provider event changed price or timestamp") from None
                return False

    def list(
        self, *, dataset: str = "live", asset: str = "ETH-USD", limit: int = 100
    ) -> list[Observation]:
        statement = (
            select(ObservationRow)
            .where(ObservationRow.dataset == dataset, ObservationRow.asset == asset)
            .order_by(ObservationRow.timestamp.desc(), ObservationRow.id.desc())
            .limit(limit)
        )
        with self.session() as session:
            return [
                Observation.model_validate_json(row.payload) for row in session.scalars(statement)
            ]
