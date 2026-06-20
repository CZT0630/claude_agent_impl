from __future__ import annotations

from types import SimpleNamespace

from agent.events import (
    MODEL_FINISHED,
    MODEL_STARTED,
    PHASE_FINISHED,
    PHASE_STARTED,
    RUN_FINISHED,
    RUN_STARTED,
    TOOL_FINISHED,
    TOOL_STARTED,
    RuntimeEvent,
)
from agent.loop import AgentLoop
from agent.runtime import AgentRuntime
from agent.state import LoopState
from tools.registry import ToolRegistry
from tools.result import ToolResult


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)

    def create(self, **_kwargs):
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def text_response(text: str = "done"):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )


def tool_response():
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(
                type="tool_use",
                id="tool_1",
                name="echo",
                input={"value": "hello"},
            )
        ],
    )


def test_runtime_event_carries_state_correlation_ids(tmp_path):
    state = LoopState.from_user_message(
        "hello",
        workdir=tmp_path,
        user_id="user-1",
        tenant_id="tenant-1",
    )
    event = RuntimeEvent.from_state(
        state,
        "example",
        sequence=1,
        payload={"path": tmp_path},
        artifact_refs=["artifact-1"],
    )
    data = event.to_dict()

    assert data["session_id"] == state.session_id
    assert data["turn_id"] == state.turn_id
    assert data["run_id"] == state.run_id
    assert data["trace_id"] == state.trace_id
    assert data["request_id"] == state.request_id
    assert data["tenant_id"] == "tenant-1"
    assert data["user_id"] == "user-1"
    assert data["payload"]["path"] == str(tmp_path)
    assert data["artifact_refs"] == ["artifact-1"]


def test_agent_runtime_emits_run_and_phase_events(tmp_path):
    class FakeLoop:
        def run(self, messages, state=None):
            messages.append({"role": "assistant", "content": "ok"})
            return messages

    state = LoopState.from_user_message("hello", workdir=tmp_path)
    AgentRuntime(FakeLoop()).execute(state)

    event_types = [event["type"] for event in state.events]
    assert event_types[0] == RUN_STARTED
    assert PHASE_STARTED in event_types
    assert PHASE_FINISHED in event_types
    assert event_types[-1] == RUN_FINISHED
    assert state.events[-1]["payload"]["status"] == "completed"


def test_agent_loop_emits_model_events():
    client = FakeClient([text_response()])
    loop = AgentLoop(
        client=client,
        model="test-model",
        system_prompt="system",
        tool_registry=ToolRegistry(),
    )
    state = LoopState.from_user_message(
        "hello",
        messages=[{"role": "user", "content": "hello"}],
    )

    loop.run(state.messages, state=state)

    model_events = [
        event for event in state.events if event["type"] in {MODEL_STARTED, MODEL_FINISHED}
    ]
    assert [event["type"] for event in model_events] == [
        MODEL_STARTED,
        MODEL_FINISHED,
    ]
    assert model_events[0]["payload"]["model"] == "test-model"
    assert model_events[1]["payload"]["stop_reason"] == "end_turn"


def test_agent_loop_emits_tool_events_with_tool_result_and_artifacts():
    registry = ToolRegistry()
    registry.register(
        "echo",
        "echo a value",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        lambda value: ToolResult.success(
            f"echo:{value}",
            artifact_refs=["artifact://tool-output"],
        ),
    )
    client = FakeClient([tool_response(), text_response()])
    loop = AgentLoop(
        client=client,
        model="test-model",
        system_prompt="system",
        tool_registry=registry,
    )
    state = LoopState.from_user_message(
        "hello",
        messages=[{"role": "user", "content": "hello"}],
    )

    loop.run(state.messages, state=state)

    tool_events = [
        event for event in state.events if event["type"] in {TOOL_STARTED, TOOL_FINISHED}
    ]
    assert [event["type"] for event in tool_events] == [
        TOOL_STARTED,
        TOOL_FINISHED,
    ]
    assert tool_events[0]["payload"]["tool_use_id"] == "tool_1"
    assert tool_events[1]["payload"]["result"]["ok"] is True
    assert tool_events[1]["payload"]["result"]["stdout"] == "echo:hello"
    assert tool_events[1]["artifact_refs"] == ["artifact://tool-output"]
