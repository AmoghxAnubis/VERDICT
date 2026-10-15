from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from verdict.models import Observation


def test_price_is_exact_and_utc_is_normalized(observation):
    data = observation.model_dump()
    data["timestamp"] = datetime(2026, 10, 16, 5, 30, tzinfo=timezone(timedelta(hours=5.5)))
    normalized = Observation.model_validate(data)
    assert normalized.timestamp == datetime(2026, 10, 16, tzinfo=UTC)
    assert normalized.value == Decimal("3843.010000000000000001")
    assert normalized.model_dump(mode="json")["value"] == "3843.010000000000000001"


@pytest.mark.parametrize(
    "price", [0, -1, "NaN", "Infinity", 3843.01, True, "0.0000000000000000001"]
)
def test_invalid_prices_are_rejected(observation, price):
    with pytest.raises(ValidationError):
        Observation.model_validate({**observation.model_dump(), "value": price})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timestamp", "2026-10-16T00:00:00"),
        ("received_at", "2026-10-16T00:00:00"),
        ("dataset", "replay:../live"),
        ("asset", "ETH-USDT"),
        ("latency_ms", -1),
        ("latency_ms", float("nan")),
    ],
)
def test_invalid_observation_fields_are_rejected(observation, field, value):
    with pytest.raises(ValidationError):
        Observation.model_validate({**observation.model_dump(), field: value})


def test_identity_separates_live_and_replay_but_not_receipt_time(observation):
    live = observation.model_copy(update={"dataset": "live"})
    later = observation.model_copy(update={"received_at": datetime(2026, 10, 17, tzinfo=UTC)})
    assert observation.observation_id != live.observation_id
    assert observation.observation_id == later.observation_id
