"""
后台任务管理器 — s13: 用线程实现异步执行，完成后注入通知

问题: subprocess.run() 是同步阻塞的，耗时命令会卡住整个 agent 循环。
方案: 用 threading.Thread 把命令丢到后台跑，主循环继续思考别的事。
      每轮循环开头自动 collect()，把完成的任务以 <task_notification> 注入 messages。

架构:
    agent_loop
        ↓ LLM 决定: bash(command="npm install", run_in_background=true)
        ↓
    BackgroundManager.start(command)
        → 生成 bg_id (如 "bg_1686000001_0001")
        → 启动 daemon 线程执行 subprocess.run()
        → 立即返回 bg_id，不等结果
        ↓
    Agent 继续思考别的事（不阻塞）
        ↓
    下一轮循环开头 → BackgroundManager.collect()
        → 扫描 status="completed" 或 "failed" 的任务
        → 生成 <task_notification> 文本，从队列移除
        → 注入 messages，LLM 自然看到结果
"""

import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BackgroundTask:
    """单个后台任务的状态记录"""
    bg_id: str                    # 唯一标识，如 "bg_1686000001_0001"
    command: str                  # 要执行的 shell 命令
    status: str = "running"       # running → completed / failed
    output: str = ""              # 命令输出（stdout + stderr 拼接）
    started_at: float = field(default_factory=time.time)  # 任务开始时间
    finished_at: float | None = None  # 任务结束时间


class BackgroundManager:
    """
    管理后台执行的 shell 命令。

    核心设计:
    - 一个命令一个线程，互不阻塞
    - threading.Lock 保证多线程写 _tasks 字典时线程安全
    - daemon=True: 主进程退出时子线程自动清理，不留孤儿进程
    - 输出截断 50000 字符，与 s08 的 L3 tool_result_budget 对齐
    """

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self._tasks: dict[str, BackgroundTask] = {}   # bg_id → 任务记录
        self._lock = threading.Lock()                  # 保护 _tasks 的线程锁
        self._counter = 0                              # 自增计数器，用于生成唯一 ID

    def _next_id(self) -> str:
        """生成唯一 ID: bg_{时间戳}_{序号}"""
        self._counter += 1
        return f"bg_{int(time.time())}_{self._counter:04d}"

    def start(self, command: str, timeout: int = 300) -> str:
        """
        在后台线程中执行命令，立即返回 bg_id。

        流程:
        1. 生成唯一 bg_id
        2. 创建 BackgroundTask 记录（status="running"）
        3. 启动 daemon 线程执行 subprocess.run()
        4. 立即返回 bg_id（不等结果）

        Args:
            command: 要执行的 shell 命令
            timeout: 最大执行时间（秒），默认 300s（5 分钟）

        Returns:
            bg_id，如 "bg_1686000001_0001"
        """
        bg_id = self._next_id()
        task = BackgroundTask(bg_id=bg_id, command=command)
        self._tasks[bg_id] = task

        def run():
            """线程内部: 执行命令，更新任务状态"""
            try:
                r = subprocess.run(
                    command, shell=True, cwd=self.workdir,
                    capture_output=True, text=True, timeout=timeout,
                )
                output = (r.stdout + r.stderr).strip()
                with self._lock:  # 加锁：多线程同时写 _tasks 时不会冲突
                    task.output = output[:50000] if output else "(no output)"
                    task.status = "completed"
                    task.finished_at = time.time()
            except subprocess.TimeoutExpired:
                with self._lock:
                    task.output = f"Error: Timeout ({timeout}s)"
                    task.status = "failed"
                    task.finished_at = time.time()
            except (FileNotFoundError, OSError) as e:
                with self._lock:
                    task.output = f"Error: {e}"
                    task.status = "failed"
                    task.finished_at = time.time()

        # daemon=True: 主进程退出时线程自动死，不留孤儿
        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        return bg_id

    def collect(self) -> list[str]:
        """
        收集已完成的后台任务，返回通知消息列表。

        被 agent loop 每轮开头调用。扫描所有 status="completed" 或 "failed"
        的任务，生成 <task_notification> 格式的文本，从队列中移除。

        Returns:
            通知文本列表，每个元素形如:
            <task_notification id='bg_xxx' status='completed'>
            ✅ Background task bg_xxx (12.3s): npm install
            Output:
            ...
            </task_notification>
        """
        notifications = []
        with self._lock:
            # 找出所有已完成或失败的任务
            done_ids = [
                bg_id for bg_id, task in self._tasks.items()
                if task.status in ("completed", "failed")
            ]
            for bg_id in done_ids:
                task = self._tasks.pop(bg_id)  # 取出并移除，避免重复通知
                icon = "✅" if task.status == "completed" else "❌"
                elapsed = ""
                if task.finished_at:
                    elapsed = f" ({task.finished_at - task.started_at:.1f}s)"
                notifications.append(
                    f"<task_notification id='{bg_id}' status='{task.status}'>\n"
                    f"{icon} Background task {bg_id}{elapsed}: {task.command}\n"
                    f"Output:\n{task.output}\n"
                    f"</task_notification>"
                )
        return notifications

    def status(self) -> str:
        """返回所有后台任务的状态摘要（供 check_background 工具调用）"""
        if not self._tasks:
            return "No background tasks running."

        lines = []
        for bg_id, task in self._tasks.items():
            elapsed = time.time() - task.started_at
            icon = {"running": "🔄", "completed": "✅", "failed": "❌"}[task.status]
            lines.append(f"  {icon} {bg_id} [{task.status}] {task.command} ({elapsed:.0f}s)")
        return "\n".join(lines)

    def get_task(self, bg_id: str) -> BackgroundTask | None:
        """获取指定后台任务（供调试用）"""
        return self._tasks.get(bg_id)

    @property
    def has_running(self) -> bool:
        """是否有正在运行的后台任务"""
        return any(t.status == "running" for t in self._tasks.values())

    @property
    def running_count(self) -> int:
        """正在运行的后台任务数"""
        return sum(1 for t in self._tasks.values() if t.status == "running")


# ── 工具 Schema ──────────────────────────────────────────────────

CHECK_BACKGROUND_SCHEMA = {
    "name": "check_background",
    "description": "Check the status of background tasks. Returns notifications for completed tasks.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}
