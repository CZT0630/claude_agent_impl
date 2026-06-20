"""Structured runtime events for agent execution.

s24 separates system-facing facts from user-facing text. RuntimeEvent is the
small event shape that later persistence, observability, UI streaming, and audit
layers can consume.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


EventSeverity = Literal["debug", "info", "warning", "error"]

RUN_STARTED = "run_started"
RUN_FINISHED = "run_finished"
RUN_FAILED = "run_failed"
PHASE_STARTED = "phase_started"
PHASE_FINISHED = "phase_finished"
MODEL_STARTED = "model_started"
MODEL_FINISHED = "model_finished"
TOOL_STARTED = "tool_started"
TOOL_FINISHED = "tool_finished"
APPROVAL_REQUIRED = "approval_required"


def _new_event_id() -> str:
    return f"event_{uuid4().hex}"


@dataclass(slots=True)
class RuntimeEvent:
    type: str
    session_id: str
    turn_id: str
    run_id: str
    trace_id: str
    request_id: str
    sequence: int
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=_new_event_id)
    parent_run_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    phase: str | None = None
    severity: EventSeverity = "info"
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    retryable: bool | None = None
    artifact_refs: list[str] = field(default_factory=list)

    @classmethod
    def from_state(
        cls,
        state: Any,
        event_type: str,
        *,
        sequence: int,
        phase: str | None = None,
        severity: EventSeverity = "info",
        message: str = "",
        payload: dict[str, Any] | None = None,
        error_code: str | None = None,
        retryable: bool | None = None,
        artifact_refs: list[str] | None = None,
    ) -> "RuntimeEvent":
        return cls(
            type=event_type,
            session_id=state.session_id,
            turn_id=state.turn_id,
            run_id=state.run_id,
            trace_id=state.trace_id,
            request_id=state.request_id,
            parent_run_id=state.parent_run_id,
            tenant_id=state.tenant_id,
            user_id=state.user_id,
            phase=phase if phase is not None else state.current_phase,
            sequence=sequence,
            severity=severity,
            message=message,
            payload=payload or {},
            error_code=error_code,
            retryable=retryable,
            artifact_refs=artifact_refs or [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {item.name: _json_safe(getattr(self, item.name)) for item in fields(self)}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Exception):
        return {"type": type(value).__name__, "message": str(value)}
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
