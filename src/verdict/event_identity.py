"""Event-content comparisons without changing retained provider evidence."""

from datetime import timedelta

from verdict.models import Observation


def same_event_content(previous: Observation, incoming: Observation) -> bool:
    if previous.observation_id != incoming.observation_id:
        return False
    if (previous.value, previous.event_sequence) != (incoming.value, incoming.event_sequence):
        return False
    if previous.timestamp == incoming.timestamp:
        return True
    if previous.source != "coinbase":
        return False

    # Observed Exchange REST timestamps can round one microsecond above the
    # corresponding matches timestamp. Apply this only across these transports,
    # never to two revisions from the same transport or another provider.
    def transport(observation: Observation) -> str | None:
        payload = observation.raw_payload
        if (
            payload.get("type") in ("match", "last_match")
            and payload.get("product_id") == "ETH-USD"
        ):
            return "websocket"
        if "type" not in payload and "product_id" not in payload:
            return "rest"
        return None

    kinds = {transport(previous), transport(incoming)}
    return kinds == {"rest", "websocket"} and abs(
        previous.timestamp - incoming.timestamp
    ) <= timedelta(microseconds=1)
