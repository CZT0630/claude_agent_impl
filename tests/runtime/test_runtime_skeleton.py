from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent.interceptor import InterceptorChain, ModelInterceptor, ToolInterceptor
from agent.loop import AgentLoop
from agent.runtime import AgentRuntime
from agent.state import LoopState
from tools.registry import ToolRegistry


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def text_response(text: str = "done"):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
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


def test_loop_state_collects_turn_metadata(tmp_path):
    state = LoopState.from_user_message(
        "hello",
        model="test-model",
        workdir=tmp_path,
        user_id="user-1",
        tenant_id="tenant-1",
    )

    state.record_tool_event("bash", "ok", tool_use_id="tool_1", ok=True)
    state.finish()

    assert state.run_id.startswith("run_")
    assert state.trace_id.startswith("trace_")
    assert state.request_id.startswith("request_")
    assert state.user_id == "user-1"
    assert state.tenant_id == "tenant-1"
    assert state.user_message == "hello"
    assert state.model == "test-model"
    assert state.workdir == tmp_path
    assert state.messages == [{"role": "user", "content": "hello"}]
    assert state.has_tool_action is True
    assert state.last_tool_name == "bash"
    assert state.tool_events[0]["tool_use_id"] == "tool_1"
    assert state.ended_at is not None
    assert state.status == "completed"


def test_loop_state_serializes_for_persistence(tmp_path):
    state = LoopState.from_user_message("hello", model="test-model", workdir=tmp_path)
    state.roles = ["admin"]
    state.scopes = ["tools:run"]
    state.mark_phase("preflight")
    state.record_model_call(
        {"model": "test-model", "messages": state.messages, "tools": [1, 2], "max_tokens": 42},
        response=SimpleNamespace(stop_reason="end_turn", usage={"input_tokens": 3}),
    )
    state.record_tool_event("echo", {"nested": SimpleNamespace(value=1)}, input={"value": "x"})
    state.finish()

    data = state.to_dict()
    restored = LoopState.from_dict(data)

    assert data["workdir"] == str(tmp_path)
    assert data["model_calls"][0]["message_count"] == 1
    assert data["tool_events"][0]["result"]["nested"]["value"] == 1
    assert restored.workdir == tmp_path
    assert restored.roles == ["admin"]
    assert restored.scopes == ["tools:run"]
    assert restored.status == "completed"


def test_interceptor_chain_orders_and_wraps_tool_calls():
    calls = []

    class First(ToolInterceptor):
        def order(self):
            return 10

        def intercept_tool(self, request, handler):
            calls.append("first:before")
            result = handler(request)
            calls.append("first:after")
            return f"{result}:first"

    class Earlier(ToolInterceptor):
        def order(self):
            return 1

        def intercept_tool(self, request, handler):
            calls.append("earlier:before")
            result = handler(request)
            calls.append("earlier:after")
            return f"{result}:earlier"

    chain = InterceptorChain(tool_interceptors=[First(), Earlier()])
    result = chain.intercept_tool({"name": "x"}, lambda req: "core")

    assert result == "core:first:earlier"
    assert calls == [
        "earlier:before",
        "first:before",
        "first:after",
        "earlier:after",
    ]


def test_agent_runtime_executes_pipeline_phases(tmp_path):
    class FakeLoop:
        def run(self, messages, state=None):
            assert state is not None
            messages.append({"role": "assistant", "content": "ok"})
            return messages

    state = LoopState.from_user_message("hi", workdir=tmp_path)
    runtime = AgentRuntime(FakeLoop())
    messages = runtime.execute(state)

    assert messages[-1] == {"role": "assistant", "content": "ok"}
    assert state.metadata["phases"] == [
        "preflight",
        "build_context",
        "react_loop",
        "finalize",
    ]
    assert state.status == "completed"
    assert set(state.phase_durations) == {
        "preflight",
        "build_context",
        "react_loop",
        "finalize",
    }
    assert state.ended_at is not None


def test_agent_loop_model_calls_pass_through_model_interceptor():
    seen = []

    class CaptureModel(ModelInterceptor):
        def intercept_model(self, request, handler):
            assert request["state"] is not None
            seen.append(dict(request))
            request = dict(request)
            request["max_tokens"] = 123
            return handler(request)

    client = FakeClient([text_response()])
    registry = ToolRegistry()
    loop = AgentLoop(
        client=client,
        model="test-model",
        system_prompt="system",
        tool_registry=registry,
        interceptors=InterceptorChain(model_interceptors=[CaptureModel()]),
    )

    state = LoopState.from_user_message("hello", messages=[{"role": "user", "content": "hello"}])
    loop.run(state.messages, state=state)

    assert seen[0]["model"] == "test-model"
    assert client.messages.requests[0]["max_tokens"] == 123
    assert state.model_calls[0]["model"] == "test-model"
    assert state.model_calls[0]["stop_reason"] == "end_turn"


def test_agent_loop_tool_calls_pass_through_tool_interceptor():
    seen = []

    class WrapTool(ToolInterceptor):
        def intercept_tool(self, request, handler):
            assert request["state"] is not None
            seen.append((request["name"], request["input"]))
            return f"wrapped:{handler(request)}"

    registry = ToolRegistry()
    registry.register(
        "echo",
        "echo a value",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        lambda value: f"echo:{value}",
    )
    client = FakeClient([tool_response(), text_response()])
    loop = AgentLoop(
        client=client,
        model="test-model",
        system_prompt="system",
        tool_registry=registry,
        interceptors=InterceptorChain(tool_interceptors=[WrapTool()]),
    )
    state = LoopState.from_user_message("hello", messages=[{"role": "user", "content": "hello"}])
    messages = state.messages

    loop.run(messages, state=state)

    tool_results = messages[2]["content"]
    assert seen == [("echo", {"value": "hello"})]
    assert tool_results[0]["content"] == "wrapped:echo:hello"
    assert state.tool_events[0]["name"] == "echo"
    assert state.tool_events[0]["ok"] is True
    assert state.tool_events[0]["input"] == {"value": "hello"}


def test_cli_runtime_style_preserves_existing_history_list(tmp_path):
    class FakeLoop:
        def __init__(self):
            self.interceptors = InterceptorChain()

        def run(self, messages, state=None):
            messages.append({"role": "assistant", "content": "ok"})
            return messages

    history = [{"role": "user", "content": "hello"}]
    state = LoopState.from_user_message(
        "hello",
        model="test-model",
        workdir=Path(tmp_path),
        messages=history,
    )

    AgentRuntime(FakeLoop()).execute(state)

    assert state.messages is history
    assert history[-1] == {"role": "assistant", "content": "ok"}


def test_cli_turns_reuse_session_id_but_get_distinct_run_ids(tmp_path):
    session_id = LoopState.new_session_id()
    history = []

    history.append({"role": "user", "content": "first"})
    first = LoopState.from_user_message(
        "first",
        model="test-model",
        workdir=tmp_path,
        session_id=session_id,
        messages=history,
    )

    history.append({"role": "user", "content": "second"})
    second = LoopState.from_user_message(
        "second",
        model="test-model",
        workdir=tmp_path,
        session_id=session_id,
        messages=history,
    )

    assert first.session_id == session_id
    assert second.session_id == session_id
    assert first.turn_id != second.turn_id
    assert first.run_id != second.run_id
    assert first.messages is history
    assert second.messages is history
