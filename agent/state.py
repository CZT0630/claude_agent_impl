"""Runtime state for one agent turn.

s23 makes the loop state explicit so later phases can persist, trace, inspect,
or resume a run without scraping local variables from AgentLoop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


LoopStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@dataclass
class LoopState:
    # Correlation IDs. A session can contain many turns; one turn can be retried
    # or delegated into multiple runs. trace_id/request_id connect this state to
    # future API, worker, and observability layers.
    session_id: str = field(default_factory=lambda: _new_id("session"))
    turn_id: str = field(default_factory=lambda: _new_id("turn"))
    run_id: str = field(default_factory=lambda: _new_id("run"))
    trace_id: str = field(default_factory=lambda: _new_id("trace"))
    request_id: str = field(default_factory=lambda: _new_id("request"))
    parent_run_id: str | None = None

    # Request context.
    user_message: str = ""
    model: str = ""
    workdir: Path = field(default_factory=Path.cwd)

    # Actor and tenant context. These are not enforced yet; s27 will use them
    # for policy checks and multi-tenant isolation.
    user_id: str | None = None
    tenant_id: str | None = None
    roles: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    actor_type: str = "cli"

    # Conversation payload passed into AgentLoop.
    messages: list[Any] = field(default_factory=list)
    system_prompt: str = ""

    # Lifecycle state and phase timing.
    status: LoopStatus = "pending"
    current_phase: str | None = None
    phase_started_at: float | None = None
    phase_durations: dict[str, float] = field(default_factory=dict)

    # Last tool facts kept for quick inspection and tests.
    has_tool_action: bool = False
    has_tool_observation: bool = False
    last_tool_name: str | None = None
    last_tool_result: Any = None

    # Append-only runtime facts. s24/s25/s26 can turn these into events,
    # persistence records, and trace spans.
    assistant_output: str = ""
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    token_usage: list[dict[str, Any]] = field(default_factory=list)
    context_sources: list[dict[str, Any]] = field(default_factory=list)

    # Budget and stop controls. They are modeled here before enforcement so
    # interceptors and later runtime phases have one place to read them from.
    cancelled: bool = False
    iterations_exhausted: bool = False
    deadline_at: float | None = None
    max_iterations: int | None = None
    token_budget: int | None = None
    cost_budget: float | None = None
    tool_call_budget: int | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    error: Exception | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new_session_id(cls) -> str:
        """Create the id for a CLI/API session, not for a single turn."""
        return _new_id("session")

    @classmethod
    def from_user_message(
        cls,
        user_message: str,
        *,
        model: str = "",
        workdir: Path | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        actor_type: str = "cli",
        messages: list[Any] | None = None,
    ) -> "LoopState":
        state = cls(
            session_id=session_id or cls.new_session_id(),
            user_message=user_message,
            model=model,
            workdir=workdir or Path.cwd(),
            user_id=user_id,
            tenant_id=tenant_id,
            actor_type=actor_type,
            messages=messages if messages is not None else [],
        )
        if not state.messages and user_message:
            state.messages.append({"role": "user", "content": user_message})
        return state

    def start(self) -> None:
        if self.status == "pending":
            self.status = "running"
        self.started_at = self.started_at or time.time()

    def cancel(self) -> None:
        self.cancelled = True
        self.status = "cancelled"

    def mark_phase(self, name: str) -> None:
        """Switch runtime phase and accumulate elapsed time for the old phase."""
        now = time.time()
        if self.current_phase and self.phase_started_at is not None:
            elapsed = now - self.phase_started_at
            self.phase_durations[self.current_phase] = (
                self.phase_durations.get(self.current_phase, 0.0) + elapsed
            )
        self.current_phase = name
        self.phase_started_at = now
        self.metadata.setdefault("phases", []).append(name)
        self.metadata["current_phase"] = name

    def record_model_call(
        self,
        request: dict[str, Any],
        *,
        response: Any | None = None,
        error: Exception | None = None,
    ) -> None:
        """Append a compact, JSON-friendly model call fact."""
        usage = _json_safe(getattr(response, "usage", None))
        if usage:
            self.token_usage.append({"usage": usage, "timestamp": time.time()})

        self.model_calls.append(
            {
                "model": request.get("model"),
                "max_tokens": request.get("max_tokens"),
                "message_count": len(request.get("messages") or []),
                "tool_count": len(request.get("tools") or []),
                "ok": error is None,
                "stop_reason": getattr(response, "stop_reason", None),
                "error": _error_dict(error),
                "timestamp": time.time(),
            }
        )

    def record_tool_event(
        self,
        name: str,
        result: Any,
        *,
        tool_use_id: str | None = None,
        ok: bool | None = None,
        input: Any | None = None,
    ) -> None:
        """Append one tool execution fact and update quick-inspection fields."""
        self.has_tool_action = True
        self.has_tool_observation = True
        self.last_tool_name = name
        self.last_tool_result = result
        self.tool_events.append(
            {
                "tool_use_id": tool_use_id,
                "name": name,
                "ok": ok,
                "input": _json_safe(input),
                "result": _json_safe(result),
                "timestamp": time.time(),
            }
        )

    def finish(self, error: Exception | None = None) -> None:
        """Close the current phase and derive the final lifecycle status."""
        self.error = error
        if self.current_phase and self.phase_started_at is not None:
            now = time.time()
            self.phase_durations[self.current_phase] = (
                self.phase_durations.get(self.current_phase, 0.0)
                + now
                - self.phase_started_at
            )
            self.phase_started_at = None
        if error is not None:
            self.status = "failed"
        elif self.cancelled:
            self.status = "cancelled"
        elif self.iterations_exhausted:
            self.status = "failed"
        else:
            self.status = "completed"
        self.ended_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        """Return a persistence-friendly representation of this state."""
        data: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "error":
                data["error"] = _error_dict(value)
            else:
                data[item.name] = _json_safe(value)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopState":
        """Restore the serializable parts of a LoopState."""
        allowed = {item.name for item in fields(cls)}
        values = {k: v for k, v in data.items() if k in allowed and k != "error"}
        if "workdir" in values:
            values["workdir"] = Path(values["workdir"])
        state = cls(**values)
        state.error = None
        return state


def _error_dict(error: Exception | None) -> dict[str, str] | None:
    if error is None:
        return None
    return {"type": type(error).__name__, "message": str(error)}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Exception):
        return _error_dict(value)
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if is_dataclass(value):
        return {
            item.name: _json_safe(getattr(value, item.name))
            for item in fields(value)
        }
    if hasattr(value, "__dict__"):
        return {
            "_type": type(value).__name__,
            **{
                str(key): _json_safe(item)
                for key, item in vars(value).items()
                if not key.startswith("_")
            },
        }
    return repr(value)
