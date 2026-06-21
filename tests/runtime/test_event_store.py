from __future__ import annotations

import json

import pytest

from agent.event_store import JsonlEventStore
from agent.events import RUN_FAILED, RUN_FINISHED, RUN_STARTED
from agent.runtime import AgentRuntime
from agent.state import LoopState


def test_jsonl_event_store_persists_events_and_run_record(tmp_path):
    state = LoopState.from_user_message(
        "hello",
        model="test-model",
        workdir=tmp_path,
        session_id="session-1",
    )
    state.start()
    state.emit_event(RUN_STARTED, payload={"model": "test-model"})
    state.emit_event(
        "artifact_created",
        artifact_refs=["artifact://one"],
        payload={"kind": "log"},
    )
    state.finish()
    state.emit_event(RUN_FINISHED, payload={"status": state.status})

    store = JsonlEventStore(tmp_path / "runtime")
    store.save_state(state)

    persisted_events = store.list_events(run_id=state.run_id)
    run_record = store.get_run(state.run_id)
    raw_lines = store.events_path.read_text(encoding="utf-8").splitlines()

    assert [event["type"] for event in persisted_events] == [
        RUN_STARTED,
        "artifact_created",
        RUN_FINISHED,
    ]
    assert len(raw_lines) == 3
    assert json.loads(raw_lines[0])["session_id"] == "session-1"
    assert run_record is not None
    assert run_record["status"] == "completed"
    assert run_record["model_call_count"] == 0
    assert run_record["event_count"] == 3
    assert run_record["artifact_refs"] == ["artifact://one"]
    assert run_record["state"]["run_id"] == state.run_id


def test_jsonl_event_store_filters_and_deduplicates_events(tmp_path):
    store = JsonlEventStore(tmp_path / "runtime")
    first = LoopState.from_user_message("first", session_id="session-1", workdir=tmp_path)
    second = LoopState.from_user_message("second", session_id="session-2", workdir=tmp_path)
    first.emit_event(RUN_STARTED)
    second.emit_event(RUN_STARTED)

    assert store.append_events(first.events + second.events) == 2
    assert store.append_events(first.events) == 0

    assert len(store.list_events(session_id="session-1")) == 1
    assert len(store.list_events(turn_id=second.turn_id)) == 1
    assert len(store.list_events(event_type=RUN_STARTED)) == 2


def test_agent_runtime_persists_successful_run(tmp_path):
    class FakeLoop:
        def run(self, messages, state=None):
            messages.append({"role": "assistant", "content": "ok"})
            return messages

    store = JsonlEventStore(tmp_path / "runtime")
    state = LoopState.from_user_message("hello", workdir=tmp_path)

    AgentRuntime(FakeLoop(), event_store=store).execute(state)

    run_record = store.get_run(state.run_id)
    event_types = [event["type"] for event in store.list_events(run_id=state.run_id)]

    assert run_record is not None
    assert run_record["status"] == "completed"
    assert run_record["event_count"] == len(state.events)
    assert event_types[0] == RUN_STARTED
    assert event_types[-1] == RUN_FINISHED


def test_agent_runtime_persists_failed_run(tmp_path):
    class FailingLoop:
        def run(self, _messages, state=None):
            raise RuntimeError("boom")

    store = JsonlEventStore(tmp_path / "runtime")
    state = LoopState.from_user_message("hello", workdir=tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        AgentRuntime(FailingLoop(), event_store=store).execute(state)

    run_record = store.get_run(state.run_id)
    event_types = [event["type"] for event in store.list_events(run_id=state.run_id)]

    assert run_record is not None
    assert run_record["status"] == "failed"
    assert run_record["error"] == {"type": "RuntimeError", "message": "boom"}
    assert event_types[-1] == RUN_FAILED
