from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from verdict.models import Observation

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_seconds(value: str | int | Decimal) -> datetime:
    if isinstance(value, (float, bool)):
        raise ValueError("timestamp must preserve decimal precision")
    try:
        seconds = Decimal(value)
        if not seconds.is_finite() or seconds < 0:
            raise ValueError("invalid epoch timestamp")
        return EPOCH + timedelta(microseconds=int(seconds * 1_000_000))
    except (InvalidOperation, OverflowError, TypeError) as error:
        raise ValueError("invalid epoch timestamp") from error


def check_clock(observation: Observation, max_future_skew_seconds: float) -> Observation:
    if observation.clock_skew_seconds() > max_future_skew_seconds:
        raise ValueError("provider timestamp exceeds permitted future clock skew")
    return observation
