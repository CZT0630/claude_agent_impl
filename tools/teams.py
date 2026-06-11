"""
Agent Teams — 消息总线 + 文件邮箱 + 异步队友 + 自动认领

问题: 单个 agent 的上下文有限，复杂任务需要分工协作。
方案: Lead Agent 派生 Teammate Agent 在独立线程中运行，
      通过文件邮箱 (.mailboxes/) 异步通信。

架构:
    Lead Agent                          Teammate Agent
    +------------------+               +------------------+
    | messages=[...]   |               | messages=[task]  |
    |                  |  MessageBus   |                  |
    | spawn_teammate   | ────────────> | own agent_loop   |
    | send_message     | ────────────> |   bash/read/write|
    | check_inbox      | <──────────── |   send_message   |
    +------------------+               +------------------+
            ↑                                   |
            └── inbox ← .mailboxes/teammate.jsonl

邮箱格式: .mailboxes/{agent_name}.jsonl
    每行一个 JSON 对象: {"from": str, "type": str, "payload": dict, "timestamp": float}

自动认领:
    - 队友在 IDLE 阶段自动扫描任务看板 (tools.task.auto_claim_task)
    - 自动认领 status=pending, owner=None, 依赖已完成 的任务
    - 60 秒无任务可认领则自动关机
    - 队友拥有 list_tasks, claim_task, complete_task 工具

新工具 (Lead):
    spawn_teammate  — 派生队友线程，返回 teammate_id
    send_message    — 向队友发送消息
    check_inbox     — 检查自己的收件箱

新工具 (Teammate):
    list_tasks      — 查看任务列表
    claim_task      — 认领任务
    complete_task   — 完成任务

关键设计:
    - 队友在独立 daemon 线程中运行自己的 AgentLoop
    - 通过文件邮箱异步通信，不阻塞 lead agent
    - 队友有 10 轮安全限制
    - 队友不能再派生队友（工具集排除 spawn_teammate）
    - 队友自主认领任务，无需 Lead 逐个分配
"""

import json
import threading
import time
from pathlib import Path

from anthropic import Anthropic
from agent.loop import AgentLoop
from tools.registry import ToolRegistry
from tools.task import TaskStore, LIST_TASKS_SCHEMA, CLAIM_TASK_SCHEMA, COMPLETE_TASK_SCHEMA, make_task_handlers, auto_claim_task


# ── 消息总线 ──────────────────────────────────────────────────────────

class MessageBus:
    """
    基于文件的邮箱消息总线。

    每个 agent 有一个邮箱文件: .mailboxes/{name}.jsonl
    发送 = 追加写入目标邮箱文件
    接收 = 读取并清空自己的邮箱文件

    线程安全: 文件追加写入在 OS 层面是原子的（小消息），
    清空用 write_text("") 覆盖，由调用方保证不并发读写同一邮箱。
    """

    def __init__(self, workdir: Path):
        self.mailbox_dir = workdir / ".mailboxes"
        self.mailbox_dir.mkdir(parents=True, exist_ok=True)

    def _mailbox_path(self, agent_name: str) -> Path:
        return self.mailbox_dir / f"{agent_name}.jsonl"

    def send(self, to: str, msg_from: str, msg_type: str, payload: dict):
        """
        向指定 agent 的邮箱发送一条消息。

        Args:
            to: 目标 agent 名字
            msg_from: 发送者名字
            msg_type: 消息类型 (如 "task", "data", "shutdown")
            payload: 消息内容
        """
        entry = {
            "from": msg_from,
            "type": msg_type,
            "payload": payload,
            "timestamp": time.time(),
        }
        mailbox = self._mailbox_path(to)
        with mailbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def receive(self, agent_name: str) -> list[dict]:
        """
        读取并清空指定 agent 的邮箱。

        Returns:
            消息列表，每条包含 from, type, payload, timestamp
        """
        mailbox = self._mailbox_path(agent_name)
        if not mailbox.exists():
            return []
        try:
            text = mailbox.read_text(encoding="utf-8")
            if not text.strip():
                return []
            messages = []
            for line in text.strip().split("\n"):
                if line.strip():
                    messages.append(json.loads(line))
            # 清空邮箱（已消费）
            mailbox.write_text("", encoding="utf-8")
            return messages
        except (json.JSONDecodeError, OSError):
            return []

    def has_messages(self, agent_name: str) -> bool:
        """检查邮箱是否有未读消息"""
        mailbox = self._mailbox_path(agent_name)
        if not mailbox.exists():
            return False
        try:
            return mailbox.stat().st_size > 0
        except OSError:
            return False

    def clear(self, agent_name: str):
        """清空指定 agent 的邮箱"""
        mailbox = self._mailbox_path(agent_name)
        if mailbox.exists():
            mailbox.write_text("", encoding="utf-8")


# ── 队友管理器 ───────────────────────────────────────────────────────

class TeamManager:
    """
    管理所有队友 agent 的生命周期。

    职责:
        - 派生队友线程（独立 AgentLoop）
        - 追踪队友状态（running / completed / failed）
        - 通过 MessageBus 实现通信
        - 支持队友自动认领任务
    """

    # 队友的 system prompt
    TEAMMATE_SYSTEM = (
        "You are an autonomous teammate agent. Your workflow:\n"
        "1. When you receive a task, work on it and complete it.\n"
        "2. After completing a task, use complete_task to mark it done.\n"
        "3. When idle, you will automatically claim unclaimed tasks from the task board.\n"
        "4. If no tasks are available for 60 seconds, you will shut down automatically.\n\n"
        "Available task management tools:\n"
        "- list_tasks: See all tasks on the board\n"
        "- claim_task: Claim a specific task (auto-claim will do this for you)\n"
        "- complete_task: Mark a task as completed\n\n"
        "You can also:\n"
        "- Use send_message to communicate with the lead agent\n"
        "- Use check_inbox to receive messages\n\n"
        "If you receive a shutdown request, acknowledge it and stop. "
        "If you receive a plan request, respond with your proposed plan. "
        "You have a limited number of rounds per task — work efficiently. "
        "Do not spawn further teammates."
    )

    def __init__(
        self,
        client: Anthropic,
        model: str,
        workdir: Path,
        parent_registry: ToolRegistry,
        task_store: TaskStore | None = None,
        max_tokens: int = 8000,
    ):
        self.client = client
        self.model = model
        self.workdir = workdir
        self.parent_registry = parent_registry
        self.max_tokens = max_tokens
        self.bus = MessageBus(workdir)
        self.task_store = task_store  # 任务存储，用于自动认领

        self._teammates: dict[str, dict] = {}  # name → {thread, status, started_at}
        self._lock = threading.Lock()
        self._counter = 0

    def _next_name(self) -> str:
        self._counter += 1
        return f"teammate_{self._counter:04d}"

    def _build_teammate_registry(self, teammate_name: str) -> ToolRegistry:
        """
        构建队友的工具注册表。

        包含: bash, read_file, write_file, edit_file, glob, send_message, check_inbox,
              list_tasks, claim_task, complete_task (自动认领所需)
        排除: task (不能递归派生), spawn_teammate (不能派生队友)
        """
        excluded = {"task", "spawn_teammate"}
        registry = ToolRegistry()

        for defn, handler in self.parent_registry.iter_handlers():
            if defn["name"] not in excluded:
                registry.register(
                    name=defn["name"],
                    description=defn.get("description", ""),
                    input_schema=dict(defn.get("input_schema", {})),
                    handler=handler,
                )

        # 给队友也注册 send_message 和 check_inbox
        registry.register(
            **SEND_MESSAGE_SCHEMA,
            handler=make_send_message_handler(self.bus, teammate_name),
        )
        registry.register(
            **CHECK_INBOX_SCHEMA,
            handler=make_check_inbox_handler(self.bus, teammate_name),
        )

        # 给队友注册任务管理工具（自动认领所需）
        if self.task_store:
            task_handlers = make_task_handlers(self.task_store)
            registry.register(
                **LIST_TASKS_SCHEMA,
                handler=task_handlers["list_tasks"],
            )
            registry.register(
                **CLAIM_TASK_SCHEMA,
                handler=task_handlers["claim_task"],
            )
            registry.register(
                **COMPLETE_TASK_SCHEMA,
                handler=task_handlers["complete_task"],
            )

        return registry

    def spawn(self, name: str | None, task_description: str, rounds: int = 10, idle_timeout: float = 60.0) -> str:
        """
        派生一个队友 agent 在独立线程中执行任务。

        生命周期:
            WORK: 执行初始任务 (最多 rounds 轮)
            → 任务完成 → IDLE: 等待新消息
            → 收到新消息 → WORK: 处理消息 (最多 rounds 轮)
            → 处理完成 → IDLE
            → idle_timeout 秒无消息 → SHUTDOWN

        Args:
            name: 队友名字（可选，默认自动生成）
            task_description: 队友的任务描述
            rounds: 每个工作周期的安全轮次限制，默认 10
            idle_timeout: 空闲超时秒数，默认 60

        Returns:
            队友名字（用于后续 send_message / check_inbox）
        """
        if not name:
            name = self._next_name()

        with self._lock:
            if name in self._teammates:
                return f"Error: teammate '{name}' already exists"
            self._teammates[name] = {
                "status": "running",
                "started_at": time.time(),
                "task": task_description,
                "lifecycle": "work",
            }

        # 构建队友的工具集和 agent loop
        registry = self._build_teammate_registry(name)

        def run():
            """线程内: 队友的 agent loop (WORK → IDLE 循环)"""
            try:
                # ── WORK 阶段: 执行初始任务 ──
                sub_agent = AgentLoop(
                    client=self.client,
                    model=self.model,
                    system_prompt=self.TEAMMATE_SYSTEM,
                    tool_registry=registry,
                    max_tokens=self.max_tokens,
                    max_rounds=rounds,
                )
                initial = (
                    f"Your name is '{name}'. You are a teammate agent.\n\n"
                    f"Your task:\n{task_description}\n\n"
                    f"When done, use send_message to report results to 'lead'."
                )
                messages = [{"role": "user", "content": initial}]
                sub_agent.run(messages)

                with self._lock:
                    self._teammates[name]["lifecycle"] = "idle"

                # ── IDLE 阶段: 等待新消息 + 自动认领任务 ──
                idle_start = time.time()
                while True:
                    # 1. 检查是否有新消息
                    inbox = self.bus.receive(name)
                    if inbox:
                        idle_start = time.time()  # 重置空闲计时

                        # 处理协议消息
                        from tools.team_protocols import handle_protocol_message, is_protocol_message
                        for msg in inbox:
                            if is_protocol_message(msg):
                                result = handle_protocol_message(msg, self.bus, name)
                                if result and "shutdown" in result.lower():
                                    # 收到关机请求，退出
                                    with self._lock:
                                        self._teammates[name]["lifecycle"] = "shutdown"
                                        self._teammates[name]["status"] = "completed"
                                        self._teammates[name]["finished_at"] = time.time()
                                    return
                            else:
                                # 普通消息，进入 WORK 阶段处理
                                with self._lock:
                                    self._teammates[name]["lifecycle"] = "work"

                                content = msg["payload"].get("content", json.dumps(msg["payload"]))
                                sub_agent2 = AgentLoop(
                                    client=self.client,
                                    model=self.model,
                                    system_prompt=self.TEAMMATE_SYSTEM,
                                    tool_registry=registry,
                                    max_tokens=self.max_tokens,
                                    max_rounds=rounds,
                                )
                                new_messages = [
                                    {"role": "user", "content": f"New message from {msg['from']}:\n{content}"}
                                ]
                                sub_agent2.run(new_messages)

                                with self._lock:
                                    self._teammates[name]["lifecycle"] = "idle"
                                idle_start = time.time()  # 重置空闲计时

                    # 2. 自动扫描任务看板，认领未分配的任务
                    if self.task_store:
                        claimed_id = auto_claim_task(self.task_store, name)
                        if claimed_id:
                            task = self.task_store.load(claimed_id)

                            with self._lock:
                                self._teammates[name]["lifecycle"] = "work_claimed"
                                self._teammates[name]["current_task"] = task.id

                            # 执行认领的任务
                            sub_agent3 = AgentLoop(
                                client=self.client,
                                model=self.model,
                                system_prompt=self.TEAMMATE_SYSTEM,
                                tool_registry=registry,
                                max_tokens=self.max_tokens,
                                max_rounds=rounds,
                            )
                            task_messages = [
                                {
                                    "role": "user",
                                    "content": (
                                        f"You have auto-claimed a task from the task board.\n\n"
                                        f"Task ID: {task.id}\n"
                                        f"Subject: {task.subject}\n"
                                        f"Description: {task.description}\n\n"
                                        f"Complete this task, then use complete_task('{task.id}') to mark it done. "
                                        f"After completing, report results to 'lead' via send_message."
                                    ),
                                }
                            ]
                            sub_agent3.run(task_messages)

                            with self._lock:
                                self._teammates[name]["lifecycle"] = "idle"
                                self._teammates[name].pop("current_task", None)
                            idle_start = time.time()  # 重置空闲计时
                            continue  # 继续检查是否有其他任务

                    # 3. 空闲超时检查
                    if time.time() - idle_start > idle_timeout:
                        with self._lock:
                            self._teammates[name]["lifecycle"] = "timeout"
                            self._teammates[name]["status"] = "completed"
                            self._teammates[name]["finished_at"] = time.time()
                        return

                    time.sleep(3)  # 每 3 秒轮询一次

            except Exception as e:
                with self._lock:
                    self._teammates[name]["status"] = "failed"
                    self._teammates[name]["error"] = str(e)
                    self._teammates[name]["finished_at"] = time.time()

            # 队友结束后，发一条自动通知给 lead
            with self._lock:
                status = self._teammates[name]["status"]
            self.bus.send(
                to="lead",
                msg_from=name,
                msg_type="teammate_done",
                payload={"status": status, "task": task_description},
            )

        thread = threading.Thread(target=run, daemon=True, name=f"teammate-{name}")
        thread.start()

        with self._lock:
            self._teammates[name]["thread"] = thread

        return name

    def status(self) -> str:
        """返回所有队友的状态摘要"""
        with self._lock:
            if not self._teammates:
                return "No teammates."

            lines = []
            for name, info in self._teammates.items():
                icon = {"running": "🔄", "completed": "✅", "failed": "❌"}.get(
                    info["status"], "❓"
                )
                lifecycle = info.get("lifecycle", "")
                lifecycle_tag = f" ({lifecycle})" if lifecycle else ""
                elapsed = time.time() - info["started_at"]
                task_preview = info["task"][:60]
                lines.append(f"  {icon} {name} [{info['status']}{lifecycle_tag}] {task_preview}... ({elapsed:.0f}s)")
            return "\n".join(lines)

    @property
    def has_running(self) -> bool:
        return any(t["status"] == "running" for t in self._teammates.values())


# ── 工具 Schema ──────────────────────────────────────────────────────

SPAWN_TEAMMATE_SCHEMA = {
    "name": "spawn_teammate",
    "description": (
        "Spawn a teammate agent to work on a subtask in a background thread. "
        "The teammate has its own agent loop with tools (bash, read, write, etc). "
        "It communicates back via send_message/check_inbox. "
        "Returns the teammate name for later reference."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Teammate name (auto-generated if omitted).",
            },
            "task": {
                "type": "string",
                "description": "The task description for the teammate to complete.",
            },
            "rounds": {
                "type": "integer",
                "description": "Max agent loop rounds for the teammate (default: 10).",
            },
        },
        "required": ["task"],
    },
}

SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to another agent's mailbox. "
        "Use 'lead' to send to the lead agent, "
        "or a teammate name to send to a teammate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Target agent name ('lead' or a teammate name).",
            },
            "message": {
                "type": "string",
                "description": "The message content to send.",
            },
            "msg_type": {
                "type": "string",
                "description": "Message type tag (default: 'message').",
            },
        },
        "required": ["to", "message"],
    },
}

CHECK_INBOX_SCHEMA = {
    "name": "check_inbox",
    "description": (
        "Check your mailbox for incoming messages. "
        "Returns all unread messages since last check."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ── 工具 Handler ─────────────────────────────────────────────────────

def make_spawn_teammate_handler(team_manager: TeamManager):
    """构建 spawn_teammate 工具的 handler"""

    def run_spawn_teammate(task: str, name: str | None = None, rounds: int = 10) -> str:
        result_name = team_manager.spawn(name, task, rounds)
        if result_name.startswith("Error"):
            return result_name
        return (
            f"Spawned teammate '{result_name}' with task:\n{task[:200]}\n\n"
            f"Use send_message(to='{result_name}', message=...) to communicate.\n"
            f"Use check_inbox() to receive their messages."
        )

    return run_spawn_teammate


def make_send_message_handler(bus: MessageBus, sender_name: str):
    """构建 send_message 工具的 handler"""

    def run_send_message(to: str, message: str, msg_type: str = "message") -> str:
        bus.send(to=to, msg_from=sender_name, msg_type=msg_type, payload={"content": message})
        return f"Sent message to '{to}'."

    return run_send_message


def make_check_inbox_handler(bus: MessageBus, agent_name: str):
    """构建 check_inbox 工具的 handler"""

    def run_check_inbox() -> str:
        messages = bus.receive(agent_name)
        if not messages:
            return "Inbox is empty."

        lines = []
        for msg in messages:
            ts = time.strftime("%H:%M:%S", time.localtime(msg["timestamp"]))
            content = msg["payload"].get("content", json.dumps(msg["payload"]))
            lines.append(f"[{ts}] from={msg['from']} type={msg['type']}: {content}")
        return "\n".join(lines)

    return run_check_inbox


def make_team_status_handler(team_manager: TeamManager):
    """构建 team_status 工具的 handler"""

    def run_team_status() -> str:
        return team_manager.status()

    return run_team_status


TEAM_STATUS_SCHEMA = {
    "name": "team_status",
    "description": "Show status of all teammate agents.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}
