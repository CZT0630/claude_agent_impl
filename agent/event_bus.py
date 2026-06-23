"""In-process event publishing and observability subscribers.

s26 keeps event distribution local and synchronous. It gives the runtime a
publish/subscribe boundary before adding external transports such as SSE,
Redis Streams, Kafka, or OpenTelemetry exporters.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from uuid import uuid4

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


RuntimeEventDict = dict[str, Any]
EventHandler = Callable[[RuntimeEventDict], None]
WILDCARD_EVENT = "*"


@dataclass(slots=True)
class EventDispatchError:
    """Structured record for subscriber failures during event dispatch."""

    event_id: str | None
    event_type: str
    handler: str
    error_type: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "handler": self.handler,
            "error_type": self.error_type,
            "message": self.message,
        }


class InProcessEventBus:
    """Synchronous pub/sub dispatcher for RuntimeEvent dictionaries."""

    def __init__(self):
        # Keep dispatch errors observable without letting subscriber failures
        # interrupt the runtime's core execution path.
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)
        self.errors: list[dict[str, Any]] = []

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for one event type or WILDCARD_EVENT."""

        self._subscribers[event_type].append(handler)

    def subscribe_many(
        self,
        event_types: Iterable[str],
        handler: EventHandler,
    ) -> None:
        for event_type in event_types:
            self.subscribe(event_type, handler)

    def publish(self, event: RuntimeEventDict) -> int:
        """Synchronously deliver an event and return successful deliveries."""

        # Exact-type subscribers run before wildcard subscribers so specialized
        # handlers see the event before broad observers such as tracing.
        handlers = [
            *self._subscribers.get(str(event.get("type")), []),
            *self._subscribers.get(WILDCARD_EVENT, []),
        ]
        delivered = 0
        for handler in handlers:
            try:
                handler(event)
                delivered += 1
            except Exception as exc:
                # Observability subscribers are side effects; record their
                # failures and continue dispatching to the remaining handlers.
                self.errors.append(_dispatch_error(event, handler, exc).to_dict())
        return delivered

    def subscriber_count(self, event_type: str | None = None) -> int:
        if event_type is not None:
            return len(self._subscribers.get(event_type, []))
        return sum(len(handlers) for handlers in self._subscribers.values())


class EventPublisher:
    """Runtime-facing publisher that hides how events are distributed."""

    def __init__(self, bus: InProcessEventBus | None = None):
        # A missing bus makes publishing a no-op, which lets tests or minimal
        # runtimes emit events without enabling the distribution layer.
        self.bus = bus
        self.published_count = 0
        self.delivered_count = 0

    def publish(self, event: RuntimeEventDict) -> int:
        self.published_count += 1
        if self.bus is None:
            return 0
        delivered = self.bus.publish(event)
        self.delivered_count += delivered
        return delivered


class RuntimeMetrics:
    """Small in-memory metrics subscriber derived from runtime events."""

    def __init__(self):
        self.event_count = 0
        self.run_started_count = 0
        self.run_finished_count = 0
        self.run_failed_count = 0
        self.model_started_count = 0
        self.model_finished_count = 0
        self.model_failed_count = 0
        self.tool_started_count = 0
        self.tool_finished_count = 0
        self.tool_failed_count = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def handle(self, event: RuntimeEventDict) -> None:
        # Metrics are derived from facts already emitted by the runtime. The
        # AgentLoop should not maintain these counters directly.
        self.event_count += 1
        event_type = event.get("type")
        if event_type == RUN_STARTED:
            self.run_started_count += 1
        elif event_type == RUN_FINISHED:
            self.run_finished_count += 1
        elif event_type == RUN_FAILED:
            self.run_failed_count += 1
        elif event_type == MODEL_STARTED:
            self.model_started_count += 1
        elif event_type == MODEL_FINISHED:
            self.model_finished_count += 1
            if event.get("error_code") or event.get("severity") == "error":
                self.model_failed_count += 1
            self._record_usage(event)
        elif event_type == TOOL_STARTED:
            self.tool_started_count += 1
        elif event_type == TOOL_FINISHED:
            self.tool_finished_count += 1
            payload = event.get("payload") or {}
            if payload.get("ok") is False or event.get("error_code"):
                self.tool_failed_count += 1

    def snapshot(self) -> dict[str, int]:
        return {
            "event_count": self.event_count,
            "run_started_count": self.run_started_count,
            "run_finished_count": self.run_finished_count,
            "run_failed_count": self.run_failed_count,
            "model_started_count": self.model_started_count,
            "model_finished_count": self.model_finished_count,
            "model_failed_count": self.model_failed_count,
            "tool_started_count": self.tool_started_count,
            "tool_finished_count": self.tool_finished_count,
            "tool_failed_count": self.tool_failed_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    def _record_usage(self, event: RuntimeEventDict) -> None:
        payload = event.get("payload") or {}
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            return
        self.input_tokens += _int_value(usage.get("input_tokens"))
        self.output_tokens += _int_value(usage.get("output_tokens"))


class RuntimeTraceRecorder:
    """Maps start/finish runtime events into lightweight span records."""

    def __init__(self):
        self.spans: list[dict[str, Any]] = []
        self._open_runs: dict[str, dict[str, Any]] = {}
        self._open_phases: dict[tuple[str, str | None], dict[str, Any]] = {}
        # Model events do not yet carry a stable call id, so keep a per-run
        # stack and close the most recent model span first.
        self._open_models: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._open_tools: dict[tuple[str, str | None], dict[str, Any]] = {}

    def handle(self, event: RuntimeEventDict) -> None:
        # Convert lifecycle start/finish events into spans. This mirrors the
        # shape of tracing systems without coupling the runtime to one backend.
        event_type = event.get("type")
        if event_type == RUN_STARTED:
            self._open_runs[str(event.get("run_id"))] = _start_span("run", event)
        elif event_type in {RUN_FINISHED, RUN_FAILED}:
            self._finish(self._open_runs, str(event.get("run_id")), event)
        elif event_type == PHASE_STARTED:
            key = (str(event.get("run_id")), event.get("phase"))
            self._open_phases[key] = _start_span("phase", event)
        elif event_type == PHASE_FINISHED:
            key = (str(event.get("run_id")), event.get("phase"))
            self._finish(self._open_phases, key, event)
        elif event_type == MODEL_STARTED:
            self._open_models[str(event.get("run_id"))].append(_start_span("model", event))
        elif event_type == MODEL_FINISHED:
            open_models = self._open_models.get(str(event.get("run_id"))) or []
            if open_models:
                self._finish_span(open_models.pop(), event)
        elif event_type == TOOL_STARTED:
            key = (str(event.get("run_id")), _tool_use_id(event))
            self._open_tools[key] = _start_span("tool", event)
        elif event_type == TOOL_FINISHED:
            key = (str(event.get("run_id")), _tool_use_id(event))
            self._finish(self._open_tools, key, event)

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(span) for span in self.spans]

    def _finish(
        self,
        spans: dict[Any, dict[str, Any]],
        key: Any,
        event: RuntimeEventDict,
    ) -> None:
        span = spans.pop(key, None)
        if span is not None:
            self._finish_span(span, event)

    def _finish_span(self, span: dict[str, Any], event: RuntimeEventDict) -> None:
        # The finishing event decides duration and status; the starting event
        # provides the shared trace/run context and attributes.
        ended_at = event.get("timestamp")
        span["end_event_id"] = event.get("event_id")
        span["ended_at"] = ended_at
        span["duration"] = _duration(span.get("started_at"), ended_at)
        span["status"] = "error" if event.get("severity") == "error" else "ok"
        span["error_code"] = event.get("error_code")
        self.spans.append(span)


def _dispatch_error(
    event: RuntimeEventDict,
    handler: EventHandler,
    exc: Exception,
) -> EventDispatchError:
    return EventDispatchError(
        event_id=event.get("event_id"),
        event_type=str(event.get("type")),
        handler=_handler_name(handler),
        error_type=type(exc).__name__,
        message=str(exc),
    )


def _handler_name(handler: EventHandler) -> str:
    owner = getattr(handler, "__self__", None)
    name = getattr(handler, "__name__", repr(handler))
    if owner is not None:
        return f"{type(owner).__name__}.{name}"
    return str(name)


def _start_span(kind: str, event: RuntimeEventDict) -> dict[str, Any]:
    payload = event.get("payload") or {}
    # Store payload as attributes so later exporters can preserve event context.
    return {
        "span_id": f"span_{uuid4().hex}",
        "kind": kind,
        "name": _span_name(kind, event, payload),
        "trace_id": event.get("trace_id"),
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
        "run_id": event.get("run_id"),
        "phase": event.get("phase"),
        "start_event_id": event.get("event_id"),
        "started_at": event.get("timestamp"),
        "attributes": payload,
    }


def _span_name(
    kind: str,
    event: RuntimeEventDict,
    payload: dict[str, Any],
) -> str:
    if kind == "phase":
        return f"phase:{event.get('phase')}"
    if kind == "model":
        return f"model:{payload.get('model')}"
    if kind == "tool":
        return f"tool:{payload.get('name')}"
    return f"run:{event.get('run_id')}"


def _tool_use_id(event: RuntimeEventDict) -> str | None:
    payload = event.get("payload") or {}
    tool_use_id = payload.get("tool_use_id")
    return str(tool_use_id) if tool_use_id is not None else None


def _duration(started_at: Any, ended_at: Any) -> float | None:
    if not isinstance(started_at, (int, float)):
        return None
    if not isinstance(ended_at, (int, float)):
        return None
    # Defensive clamp for duplicated or out-of-order timestamps.
    return max(0.0, ended_at - started_at)


def _int_value(value: Any) -> int:
    # bool is an int subclass in Python, but True/False are not token counts.
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0
