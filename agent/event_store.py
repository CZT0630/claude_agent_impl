"""Local persistence for runtime events and run state.

s25 keeps the storage layer intentionally small: append events to JSONL for an
audit-friendly timeline, and write one JSON run record per run for quick lookup.
The interface is narrow so a later SQLite/PostgreSQL implementation can replace
this file-backed store without changing AgentRuntime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Protocol


class RuntimeEventStore(Protocol):
    def save_state(self, state: Any) -> None: ...

    def append_events(self, events: Iterable[dict[str, Any]]) -> int: ...

    def list_events(
        self,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...


class JsonlEventStore:
    """File-backed RuntimeEventStore used by the local CLI runtime."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.events_dir = self.root / "events"
        self.runs_dir = self.root / "runs"
        self.events_path = self.events_dir / "runtime-events.jsonl"

    @classmethod
    def for_workdir(cls, workdir: Path) -> "JsonlEventStore":
        return cls(Path(workdir) / "data" / "runtime")

    def save_state(self, state: Any) -> None:
        self.append_events(state.events)
        self.save_run(state)

    def append_events(self, events: Iterable[dict[str, Any]]) -> int:
        self.events_dir.mkdir(parents=True, exist_ok=True)
        seen_event_ids = self._event_ids()
        written = 0
        with self.events_path.open("a", encoding="utf-8") as fh:
            for event in events:
                event_id = event.get("event_id")
                if event_id and event_id in seen_event_ids:
                    continue
                fh.write(_json_line(event))
                seen_event_ids.add(str(event_id))
                written += 1
        return written

    def save_run(self, state: Any) -> dict[str, Any]:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        record = self._run_record(state)
        path = self.runs_dir / f"{state.run_id}.json"
        path.write_text(_json_dump(record), encoding="utf-8")
        return record

    def list_events(
        self,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []

        events: list[dict[str, Any]] = []
        with self.events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if session_id and event.get("session_id") != session_id:
                    continue
                if turn_id and event.get("turn_id") != turn_id:
                    continue
                if run_id and event.get("run_id") != run_id:
                    continue
                if event_type and event.get("type") != event_type:
                    continue
                events.append(event)
        return events

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        path = self.runs_dir / f"{run_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _event_ids(self) -> set[str]:
        if not self.events_path.exists():
            return set()
        ids: set[str] = set()
        with self.events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event_id = json.loads(line).get("event_id")
                except json.JSONDecodeError:
                    continue
                if event_id:
                    ids.add(str(event_id))
        return ids

    def _run_record(self, state: Any) -> dict[str, Any]:
        state_data = state.to_dict()
        return {
            "schema_version": 1,
            "session_id": state.session_id,
            "turn_id": state.turn_id,
            "run_id": state.run_id,
            "trace_id": state.trace_id,
            "request_id": state.request_id,
            "parent_run_id": state.parent_run_id,
            "tenant_id": state.tenant_id,
            "user_id": state.user_id,
            "actor_type": state.actor_type,
            "model": state.model,
            "workdir": str(state.workdir),
            "status": state.status,
            "started_at": state.started_at,
            "ended_at": state.ended_at,
            "phase_durations": state.phase_durations,
            "model_call_count": len(state.model_calls),
            "tool_call_count": len(state.tool_events),
            "event_count": len(state.events),
            "artifact_refs": _artifact_refs(state.events),
            "error": state_data.get("error"),
            "state": state_data,
        }


def _artifact_refs(events: Iterable[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for event in events:
        for ref in event.get("artifact_refs") or []:
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def _json_line(data: dict[str, Any]) -> str:
    return _json_dump(data) + "\n"


def _json_dump(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)
