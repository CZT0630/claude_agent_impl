"""FastAPI server that bridges the existing AgentRuntime to a browser frontend.

Usage:
    python -m web.server               # default port 8000
    python -m web.server --port 3000   # custom port
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# ── 项目根目录加入 sys.path ───────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import (
    AgentLoop,
    AgentRuntime,
    Config,
    EventPublisher,
    InProcessEventBus,
    JsonlEventStore,
    LoopState,
    RuntimeMetrics,
    RuntimeTraceRecorder,
)
from main import build_agent


# ── 全局服务容器 ──────────────────────────────────────────────────────

class Session:
    """一个对话会话的状态。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.title: str = ""
        self.preview: str = ""
        self.messages: list[dict[str, Any]] = []

    def update_meta(self) -> None:
        """从 messages 中提取 title 和 preview。"""
        for msg in self.messages:
            if msg.get("role") == "user" and not self.title:
                content = msg.get("content", "")
                self.title = (content[:40] + "…") if len(content) > 40 else content
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                text = content if isinstance(content, str) else ""
                if isinstance(content, list):
                    for block in content:
                        if hasattr(block, "text"):
                            text += block.text
                self.preview = (text[:60] + "…") if len(text) > 60 else text
                break

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "preview": self.preview,
            "message_count": len(self.messages),
        }


class Services:
    """Hold all runtime services in one place."""

    config: Config
    agent: AgentLoop
    runtime: AgentRuntime
    event_bus: InProcessEventBus
    metrics: RuntimeMetrics
    trace_recorder: RuntimeTraceRecorder
    event_store: JsonlEventStore
    relay_queue: asyncio.Queue
    sessions: dict[str, Session]

    def __init__(self) -> None:
        self.sessions = {}


svc = Services()


# ── FastAPI 应用 ──────────────────────────────────────────────────────

app = FastAPI(title="Claude Agent Dashboard")

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def _startup() -> None:
    """组装运行时服务并启动 SSE 中继。"""
    svc.config = Config.from_env()
    svc.agent = build_agent(svc.config)

    svc.event_bus = InProcessEventBus()
    svc.metrics = RuntimeMetrics()
    svc.trace_recorder = RuntimeTraceRecorder()
    svc.relay_queue = asyncio.Queue(maxsize=1000)

    svc.event_bus.subscribe("*", svc.metrics.handle)
    svc.event_bus.subscribe("*", svc.trace_recorder.handle)

    def _push_to_relay(event: dict[str, Any]) -> None:
        try:
            svc.relay_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    svc.event_bus.subscribe("*", _push_to_relay)

    svc.event_store = JsonlEventStore.for_workdir(svc.config.workdir)

    svc.runtime = AgentRuntime(
        svc.agent,
        event_store=svc.event_store,
        event_publisher=EventPublisher(svc.event_bus),
    )

    asyncio.get_event_loop().create_task(_sse_relay_loop())


# ── SSE 中继 ─────────────────────────────────────────────────────────

_sse_queues: list[asyncio.Queue] = []
_sse_lock = asyncio.Lock()


async def _sse_relay_loop() -> None:
    while True:
        event = await svc.relay_queue.get()
        async with _sse_lock:
            dead: list[int] = []
            for i, q in enumerate(_sse_queues):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(i)
            for i in reversed(dead):
                _sse_queues.pop(i)


# ── 路由：前端 ────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Claude Agent Dashboard</h1><p>index.html not found</p>")


# ── 路由：Chat ────────────────────────────────────────────────────────


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        return {"error": "message is required"}

    session_id = body.get("session_id") or LoopState.new_session_id()

    # 获取或创建会话
    if session_id not in svc.sessions:
        svc.sessions[session_id] = Session(session_id)
    session = svc.sessions[session_id]

    session.messages.append({"role": "user", "content": message})
    session.update_meta()

    state = LoopState.from_user_message(
        message,
        model=svc.config.model,
        workdir=svc.config.workdir,
        session_id=session_id,
        messages=list(session.messages),
    )

    try:
        await asyncio.to_thread(svc.runtime.execute, state)
    except Exception as exc:
        return {
            "error": str(exc),
            "run_id": state.run_id,
            "session_id": session_id,
        }

    # 把 agent 产生的新消息同步到 session
    old_len = len(session.messages)
    session.messages = state.messages
    session.update_meta()

    reply = _extract_reply(state.messages)

    return {
        "reply": reply,
        "run_id": state.run_id,
        "session_id": session_id,
        "status": state.status,
    }


# ── 路由：SSE ────────────────────────────────────────────────────────


@app.get("/api/events/stream")
async def event_stream():
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    async with _sse_lock:
        _sse_queues.append(queue)

    async def _generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            async with _sse_lock:
                if queue in _sse_queues:
                    _sse_queues.remove(queue)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── 路由：Sessions ───────────────────────────────────────────────────


@app.get("/api/sessions")
async def list_sessions():
    return [s.to_dict() for s in svc.sessions.values()]


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    session = svc.sessions.get(session_id)
    if session is None:
        return {"error": "session not found"}
    return {"session_id": session_id, "messages": session.messages}


# ── 路由：Metrics / Traces / Runs ────────────────────────────────────


@app.get("/api/metrics")
async def metrics():
    return svc.metrics.snapshot()


@app.get("/api/traces")
async def traces():
    return svc.trace_recorder.snapshot()


@app.get("/api/runs")
async def list_runs():
    runs_dir = svc.event_store.runs_dir
    if not runs_dir.exists():
        return []
    runs = []
    for path in sorted(runs_dir.glob("*.json"), reverse=True):
        runs.append(json.loads(path.read_text(encoding="utf-8")))
    return runs


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    record = svc.event_store.get_run(run_id)
    if record is None:
        return {"error": "run not found"}
    return record


# ── 工具函数 ──────────────────────────────────────────────────────────


def _extract_reply(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            if parts:
                return "\n".join(parts)
    return ""


# ── CLI 入口 ──────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Claude Agent Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("web.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
