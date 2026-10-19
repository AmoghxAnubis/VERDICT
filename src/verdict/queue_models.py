"""Versioned live polling attempts, with identity independent of Redis delivery IDs."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from verdict.models import Observation, Source


class QueuedAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    attempt_id: UUID = Field(default_factory=uuid4)
    attempted_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Source
    observation: Observation | None = None
    error_type: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("queue version must be an integer")
        return value

    @classmethod
    def from_payload(cls, payload: str) -> "QueuedAttempt":
        attempt = cls.model_validate_json(payload)
        if not {"version", "attempt_id", "attempted_at", "source"} <= attempt.model_fields_set:
            # Defaults are convenient for producers, but generating them during redelivery
            # would turn one malformed message into a new attempt on every retry.
            raise ValueError("wire envelope requires version, attempt ID, time, and source")
        return attempt

    @model_validator(mode="after")
    def validate_attempt(self):
        if (self.observation is None) == (self.error_type is None):
            raise ValueError("an attempt requires exactly one observation or error")
        if self.observation is not None:
            if self.observation.dataset != "live" or self.observation.source != self.source:
                raise ValueError("queued observations must match the live source")
            if self.http_status is not None:
                raise ValueError("successful attempts cannot contain an error HTTP status")
        return self

    @property
    def content_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()
