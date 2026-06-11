"""
Task System 工具 — 文件持久化的任务图，支持依赖关系

存储: .tasks/{task_id}.json
工具: create_task, list_tasks, get_task, claim_task, complete_task
"""

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path


# ── 数据模型 ──────────────────────────────────────────────────────

@dataclass
class Task:
    id: str                          # task_{timestamp}_{seq}
    subject: str                     # 任务标题
    description: str                 # 详细描述
    status: str                      # pending | in_progress | completed
    owner: str | None = None         # Agent 名字 (多 agent 场景)
    blockedBy: list[str] = field(default_factory=list)  # 依赖的任务 ID 列表
    worktree: str | None = None      # s18: 绑定的 worktree 名称
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None


# ── 存储层 ────────────────────────────────────────────────────────

class TaskStore:
    def __init__(self, tasks_dir: Path):
        self.tasks_dir = tasks_dir
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"task_{int(time.time())}_{self._seq:04d}"

    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def save(self, task: Task):
        path = self._task_path(task.id)
        path.write_text(json.dumps(asdict(task), indent=2, ensure_ascii=False), encoding="utf-8")

    def load(self, task_id: str) -> Task | None:
        path = self._task_path(task_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Task(**data)

    def list_all(self) -> list[Task]:
        tasks = []
        for f in sorted(self.tasks_dir.glob("task_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                tasks.append(Task(**data))
            except (json.JSONDecodeError, TypeError):
                continue
        return tasks

    def delete(self, task_id: str):
        path = self._task_path(task_id)
        if path.exists():
            path.unlink()


# ── 依赖检查 ──────────────────────────────────────────────────────

def can_start(task: Task, store: TaskStore) -> bool:
    """检查所有依赖是否已完成"""
    for dep_id in task.blockedBy:
        dep = store.load(dep_id)
        if not dep or dep.status != "completed":
            return False
    return True


# ── 自动认领 ───────────────────────────────────────────────────────

def scan_unclaimed_tasks(store: TaskStore) -> list[Task]:
    """扫描未认领的、依赖已完成的 pending 任务。"""
    return [
        t for t in store.list_all()
        if t.status == "pending"
        and t.owner is None
        and can_start(t, store)
    ]


def auto_claim_task(store: TaskStore, agent_name: str) -> str | None:
    """
    自动认领一个可执行的任务。

    Returns:
        认领的任务 ID，或 None（没有可认领的任务）
    """
    unclaimed = scan_unclaimed_tasks(store)
    if not unclaimed:
        return None

    task = unclaimed[0]
    task.status = "in_progress"
    task.owner = agent_name
    store.save(task)
    return task.id


# ── 工具 Schema ───────────────────────────────────────────────────

CREATE_TASK_SCHEMA = {
    "name": "create_task",
    "description": "Create a new task. Optionally specify dependencies (blockedBy) on other tasks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Short task title.",
            },
            "description": {
                "type": "string",
                "description": "Detailed task description.",
            },
            "blockedBy": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of task IDs this task depends on.",
            },
        },
        "required": ["subject", "description"],
    },
}

LIST_TASKS_SCHEMA = {
    "name": "list_tasks",
    "description": "List all tasks, optionally filtered by status.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed"],
                "description": "Filter by status (omit to list all).",
            },
        },
        "required": [],
    },
}

GET_TASK_SCHEMA = {
    "name": "get_task",
    "description": "Get details of a specific task by ID.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID.",
            },
        },
        "required": ["task_id"],
    },
}

CLAIM_TASK_SCHEMA = {
    "name": "claim_task",
    "description": "Claim a pending task (set status to in_progress and assign owner).",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID to claim.",
            },
            "owner": {
                "type": "string",
                "description": "Name of the agent claiming this task.",
            },
        },
        "required": ["task_id"],
    },
}

COMPLETE_TASK_SCHEMA = {
    "name": "complete_task",
    "description": "Mark a task as completed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID to complete.",
            },
        },
        "required": ["task_id"],
    },
}


# ── 工具 Handler ─────────────────────────────────────────────────

def make_task_handlers(store: TaskStore):
    """返回 5 个 task 工具的 handler"""

    def run_create_task(subject: str, description: str, blockedBy: list[str] | None = None) -> str:
        # 验证依赖 ID 存在
        if blockedBy:
            for dep_id in blockedBy:
                if not store.load(dep_id):
                    return f"Error: dependency task not found: {dep_id}"

        task_id = store._next_id()
        task = Task(
            id=task_id,
            subject=subject,
            description=description,
            status="pending",
            blockedBy=blockedBy or [],
        )
        store.save(task)

        dep_info = f", blocked by: {blockedBy}" if blockedBy else ""
        return f"Created task {task_id}: {subject}{dep_info}"

    def run_list_tasks(status: str | None = None) -> str:
        tasks = store.list_all()
        if status:
            tasks = [t for t in tasks if t.status == status]

        if not tasks:
            return f"No tasks found{' with status=' + status if status else ''}."

        status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}
        lines = []
        for t in tasks:
            icon = status_icon.get(t.status, "?")
            owner = f" (owner: {t.owner})" if t.owner else ""
            blocked = f" [blocked by: {', '.join(t.blockedBy)}]" if t.blockedBy else ""
            lines.append(f"  {icon} {t.id} [{t.status}] {t.subject}{owner}{blocked}")
        return "\n".join(lines)

    def run_get_task(task_id: str) -> str:
        task = store.load(task_id)
        if not task:
            return f"Task not found: {task_id}"

        status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}
        icon = status_icon.get(task.status, "?")
        lines = [
            f"{icon} {task.id}",
            f"  Subject: {task.subject}",
            f"  Status: {task.status}",
            f"  Description: {task.description}",
        ]
        if task.owner:
            lines.append(f"  Owner: {task.owner}")
        if task.blockedBy:
            lines.append(f"  Blocked by: {', '.join(task.blockedBy)}")
        if task.completed_at:
            lines.append(f"  Completed at: {time.strftime('%Y-%m-%d %H:%M', time.localtime(task.completed_at))}")
        return "\n".join(lines)

    def run_claim_task(task_id: str, owner: str = "agent") -> str:
        task = store.load(task_id)
        if not task:
            return f"Task not found: {task_id}"
        if task.status != "pending":
            return f"Error: task {task_id} is already {task.status}, cannot claim"
        if not can_start(task, store):
            blocked = [dep_id for dep_id in task.blockedBy
                       if (d := store.load(dep_id)) is None or d.status != "completed"]
            return f"Error: task {task_id} is blocked by: {blocked}"

        task.status = "in_progress"
        task.owner = owner
        store.save(task)
        return f"Claimed task {task_id}: {task.subject} (owner: {owner})"

    def run_complete_task(task_id: str) -> str:
        task = store.load(task_id)
        if not task:
            return f"Task not found: {task_id}"
        if task.status == "completed":
            return f"Task {task_id} is already completed"

        task.status = "completed"
        task.completed_at = time.time()
        store.save(task)

        # 检查是否有其他 pending 任务现在可以开始了
        unblocked = []
        for t in store.list_all():
            if t.status == "pending" and task_id in t.blockedBy and can_start(t, store):
                unblocked.append(f"  {t.id}: {t.subject}")

        result = f"Completed task {task_id}: {task.subject}"
        if unblocked:
            result += f"\n\nUnblocked tasks:\n" + "\n".join(unblocked)
        return result

    return {
        "create_task": run_create_task,
        "list_tasks": run_list_tasks,
        "get_task": run_get_task,
        "claim_task": run_claim_task,
        "complete_task": run_complete_task,
    }
