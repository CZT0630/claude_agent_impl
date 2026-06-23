from __future__ import annotations

from agent.event_bus import (
    EventPublisher,
    InProcessEventBus,
    RuntimeMetrics,
    RuntimeTraceRecorder,
    WILDCARD_EVENT,
)
from agent.events import (
    MODEL_FINISHED,
    MODEL_STARTED,
    PHASE_FINISHED,
    PHASE_STARTED,
    RUN_FAILED,
    RUN_FINISHED,
    RUN_STARTED,
    TOOL_FINISHED,
    TOOL_STARTED,
)
from agent.runtime import AgentRuntime
from agent.state import LoopState


def event(event_type: str, **overrides):
    data = {
        "type": event_type,
        "event_id": f"event-{event_type}",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "run_id": "run-1",
        "trace_id": "trace-1",
        "request_id": "request-1",
        "phase": "react_loop",
        "timestamp": 10.0,
        "severity": "info",
        "payload": {},
        "error_code": None,
    }
    data.update(overrides)
    return data


def test_in_process_event_bus_dispatches_specific_and_wildcard_handlers():
    bus = InProcessEventBus()
    calls = []

    bus.subscribe(TOOL_FINISHED, lambda item: calls.append(("tool", item["type"])))
    bus.subscribe(WILDCARD_EVENT, lambda item: calls.append(("all", item["type"])))

    delivered = bus.publish(event(TOOL_FINISHED))

    assert delivered == 2
    assert calls == [("tool", TOOL_FINISHED), ("all", TOOL_FINISHED)]


def test_in_process_event_bus_records_handler_errors_and_continues():
    bus = InProcessEventBus()
    calls = []

    def broken(_event):
        raise RuntimeError("subscriber failed")

    bus.subscribe(RUN_FINISHED, broken)
    bus.subscribe(RUN_FINISHED, lambda item: calls.append(item["type"]))

    delivered = bus.publish(event(RUN_FINISHED))

    assert delivered == 1
    assert calls == [RUN_FINISHED]
    assert bus.errors == [
        {
            "event_id": "event-run_finished",
            "event_type": RUN_FINISHED,
            "handler": "broken",
            "error_type": "RuntimeError",
            "message": "subscriber failed",
        }
    ]


def test_loop_state_emit_event_publishes_to_attached_publisher(tmp_path):
    bus = InProcessEventBus()
    seen = []
    bus.subscribe(WILDCARD_EVENT, seen.append)
    publisher = EventPublisher(bus)
    state = LoopState.from_user_message("hello", workdir=tmp_path)
    state.event_publisher = publisher

    emitted = state.emit_event("example", payload={"value": 1})

    assert seen == [emitted]
    assert state.events == [emitted]
    assert publisher.published_count == 1
    assert publisher.delivered_count == 1


def test_runtime_metrics_subscriber_counts_events_and_tokens():
    metrics = RuntimeMetrics()
    events = [
        event(RUN_STARTED),
        event(MODEL_STARTED),
        event(
            MODEL_FINISHED,
            payload={"usage": {"input_tokens": 3, "output_tokens": 5}},
        ),
        event(TOOL_STARTED),
        event(TOOL_FINISHED, payload={"ok": False}, error_code="TOOL_FAILED"),
        event(RUN_FAILED, severity="error", error_code="RuntimeError"),
    ]

    for item in events:
        metrics.handle(item)

    assert metrics.snapshot() == {
        "event_count": 6,
        "run_started_count": 1,
        "run_finished_count": 0,
        "run_failed_count": 1,
        "model_started_count": 1,
        "model_finished_count": 1,
        "model_failed_count": 0,
        "tool_started_count": 1,
        "tool_finished_count": 1,
        "tool_failed_count": 1,
        "input_tokens": 3,
        "output_tokens": 5,
    }


def test_runtime_trace_recorder_maps_events_to_spans():
    recorder = RuntimeTraceRecorder()
    events = [
        event(RUN_STARTED, timestamp=1.0, phase=None),
        event(PHASE_STARTED, timestamp=2.0, phase="react_loop"),
        event(MODEL_STARTED, timestamp=3.0, payload={"model": "test-model"}),
        event(MODEL_FINISHED, timestamp=4.5, payload={"model": "test-model"}),
        event(TOOL_STARTED, timestamp=5.0, payload={"name": "echo", "tool_use_id": "tool-1"}),
        event(TOOL_FINISHED, timestamp=7.0, payload={"name": "echo", "tool_use_id": "tool-1"}),
        event(PHASE_FINISHED, timestamp=8.0, phase="react_loop"),
        event(RUN_FINISHED, timestamp=10.0, phase="finalize"),
    ]

    for item in events:
        recorder.handle(item)

    spans = recorder.snapshot()
    assert [(span["kind"], span["name"], span["duration"]) for span in spans] == [
        ("model", "model:test-model", 1.5),
        ("tool", "tool:echo", 2.0),
        ("phase", "phase:react_loop", 6.0),
        ("run", "run:run-1", 9.0),
    ]


def test_agent_runtime_publishes_lifecycle_events(tmp_path):
    class FakeLoop:
        def run(self, messages, state=None):
            assert state is not None
            state.emit_event("loop_observed")
            messages.append({"role": "assistant", "content": "ok"})
            return messages

    bus = InProcessEventBus()
    seen = []
    bus.subscribe(WILDCARD_EVENT, seen.append)
    state = LoopState.from_user_message("hello", workdir=tmp_path)

    AgentRuntime(
        FakeLoop(),
        event_publisher=EventPublisher(bus),
    ).execute(state)

    event_types = [item["type"] for item in seen]
    assert event_types[0] == RUN_STARTED
    assert "loop_observed" in event_types
    assert event_types[-1] == RUN_FINISHED
