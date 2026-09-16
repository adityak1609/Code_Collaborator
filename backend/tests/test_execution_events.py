import json
import uuid

from app.execution.events import validate_execution_event
from app.execution.queue import EXECUTION_EVENT_CHANNEL_PREFIX


def test_execution_event_is_bound_to_channel_session():
    session_id = str(uuid.uuid4())
    event = {
        "type": "execution_status",
        "execution_id": str(uuid.uuid4()),
        "session_id": session_id,
        "status": "RUNNING",
    }

    assert validate_execution_event(
        f"{EXECUTION_EVENT_CHANNEL_PREFIX}{session_id}", json.dumps(event)
    ) == (session_id, event)


def test_execution_event_rejects_cross_session_payload():
    channel_session = str(uuid.uuid4())
    event = {
        "type": "execution_output",
        "execution_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "stream": "stdout",
        "data": "nope",
        "status": "RUNNING",
    }

    assert (
        validate_execution_event(
            f"{EXECUTION_EVENT_CHANNEL_PREFIX}{channel_session}",
            json.dumps(event),
        )
        is None
    )
