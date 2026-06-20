"""Minimal enterprise runtime skeleton for AgentLoop.

This keeps the current AgentLoop behavior intact while establishing an explicit
pipeline entrypoint that future API, worker, persistence, and tracing layers can
reuse.
"""

from __future__ import annotations

from agent.events import (
    PHASE_FINISHED,
    PHASE_STARTED,
    RUN_FAILED,
    RUN_FINISHED,
    RUN_STARTED,
)
from agent.interceptor import InterceptorChain
from agent.state import LoopState


class AgentRuntime:
    """Pipeline wrapper around AgentLoop.

    The runtime owns lifecycle transitions; AgentLoop still owns the ReAct
    behavior. This boundary keeps future API, worker, persistence, and tracing
    layers from calling the raw loop directly.
    """

    PHASES = ("preflight", "build_context", "react_loop", "finalize")

    def __init__(
        self,
        loop,
        *,
        interceptors: InterceptorChain | None = None,
    ):
        self.loop = loop
        self.interceptors = interceptors or getattr(loop, "interceptors", InterceptorChain())
        self.loop.interceptors = self.interceptors

    def execute(self, state: LoopState) -> list:
        """Run one turn and leave success/failure facts on LoopState."""
        try:
            state.start()
            state.emit_event(
                RUN_STARTED,
                payload={
                    "model": state.model,
                    "workdir": state.workdir,
                    "actor_type": state.actor_type,
                },
            )
            self._preflight(state)
            self._build_context(state)
            self._react_loop(state)
            return self._finalize(state)
        except Exception as exc:
            failed_phase = state.current_phase
            state.finish(error=exc)
            if failed_phase:
                state.emit_event(
                    PHASE_FINISHED,
                    phase=failed_phase,
                    severity="error",
                    payload={
                        "duration": state.phase_durations.get(failed_phase),
                        "status": state.status,
                    },
                    error_code=type(exc).__name__,
                )
            state.emit_event(
                RUN_FAILED,
                severity="error",
                message=str(exc),
                payload={"status": state.status},
                error_code=type(exc).__name__,
            )
            raise

    def _mark_phase(self, state: LoopState, name: str) -> None:
        previous = state.current_phase
        state.mark_phase(name)
        if previous:
            state.emit_event(
                PHASE_FINISHED,
                phase=previous,
                payload={
                    "duration": state.phase_durations.get(previous),
                    "next_phase": name,
                },
            )
        state.emit_event(PHASE_STARTED, phase=name)

    def _preflight(self, state: LoopState) -> None:
        # Cheap checks go here before context building or model calls spend cost.
        self._mark_phase(state, "preflight")
        if state.cancelled:
            raise RuntimeError("LoopState is cancelled")

    def _build_context(self, state: LoopState) -> None:
        # s25/s28 can extend this phase with persistence, memory, and RAG input.
        self._mark_phase(state, "build_context")
        if state.user_message and not state.messages:
            state.messages.append({"role": "user", "content": state.user_message})

    def _react_loop(self, state: LoopState) -> None:
        # The existing AgentLoop remains the execution engine for now.
        self._mark_phase(state, "react_loop")
        state.messages = self.loop.run(state.messages, state=state)

    def _finalize(self, state: LoopState) -> list:
        # 终结阶段关闭计时并计算最终生命周期状态。
        self._mark_phase(state, "finalize")
        state.finish()
        state.emit_event(
            PHASE_FINISHED,
            phase="finalize",
            payload={
                "duration": state.phase_durations.get("finalize"),
                "status": state.status,
            },
        )
        state.emit_event(
            RUN_FINISHED,
            payload={
                "status": state.status,
                "phase_durations": state.phase_durations,
                "model_call_count": len(state.model_calls),
                "tool_call_count": len(state.tool_events),
            },
        )
        return state.messages
