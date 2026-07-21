from __future__ import annotations

import pytest
from agentrelay.models import Event, EventType


def test_event_type_enum_values():
    assert EventType.TASK_CREATED.value == "task.created"
    assert EventType.JOB_STARTED.value == "job.started"
    assert EventType.JOB_COMPLETED.value == "job.completed"
    assert EventType.JOB_FAILED.value == "job.failed"
    assert EventType.FOCUS_CHANGED.value == "focus.changed"
    assert EventType.MEMORY_UPDATED.value == "memory.updated"
    assert EventType.CONFLICT_DETECTED.value == "conflict.detected"


def test_event_schema_creation_and_defaults():
    event = Event(
        event_type=EventType.TASK_CREATED,
        producer="subagent-1",
        payload={"task_id": "123", "status": "pending"},
        correlation_id="corr-456",
    )
    assert event.id is not None
    assert isinstance(event.id, str)
    assert len(event.id) > 0
    assert event.event_type == EventType.TASK_CREATED
    assert event.producer == "subagent-1"
    assert event.payload == {"task_id": "123", "status": "pending"}
    assert event.correlation_id == "corr-456"
    assert event.ts > 0
    assert event.seq is None


def test_event_serialization():
    event = Event(
        event_type=EventType.CONFLICT_DETECTED,
        producer="mindsync",
        payload={"reason": "file collision"},
    )
    dump = event.model_dump(mode="json")
    assert dump["event_type"] == "conflict.detected"
    assert dump["producer"] == "mindsync"
    assert dump["payload"] == {"reason": "file collision"}
