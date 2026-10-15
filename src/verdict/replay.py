import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from verdict.connectors.coinbase import normalize_ticker
from verdict.models import Dataset, Observation
from verdict.storage import ObservationStore


class ReplayEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    received_at: AwareDatetime
    latency_ms: float = Field(ge=0, allow_inf_nan=False)
    payload: dict


class ReplayScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str
    events: list[ReplayEvent] = Field(min_length=1)


def load_observations(path: Path) -> list[Observation]:
    scenario = ReplayScenario.model_validate_json(path.read_text(encoding="utf-8"))
    # Simulation namespace is assigned here; the fixture cannot select live storage.
    dataset: Dataset = f"replay:{scenario.name}"
    return [
        normalize_ticker(
            event.payload,
            received_at=event.received_at,
            latency_ms=event.latency_ms,
            dataset=dataset,
        )
        for event in scenario.events
    ]


def replay(path: Path, store: ObservationStore) -> dict:
    observations = load_observations(path)
    # Normalize the whole fixture before writing anything.
    inserted = sum(store.add(observation) for observation in observations)
    canonical = json.dumps(
        [observation.model_dump(mode="json") for observation in observations],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "dataset": observations[0].dataset,
        "events": len(observations),
        "inserted": inserted,
        "duplicates": len(observations) - inserted,
        "normalized_sha256": hashlib.sha256(canonical).hexdigest(),
    }
