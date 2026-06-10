"""
Cron Scheduler — s14: 独立守护线程 + 队列处理器，实现定时任务

问题: Agent 需要按时自动触发任务（如每 5 分钟检查一次部署状态），
      但 agent loop 本身是被动的——只有收到消息才运行。
方案: 用守护线程按时触发，用队列解耦调度器和 agent。

四层架构:
    Layer 1: Scheduler (守护线程)
        → 每秒检查 cron 表达式
        → 匹配时写入 cron_queue

    Layer 2: Queue (线程安全队列)
        → cron_queue 解耦调度器和 agent

    Layer 3: Queue Processor
        → agent loop 每轮开头 get_nowait() 检查

    Layer 4: Consumer (agent_loop)
        → 消费队列中的任务，注入 messages

Cron 表达式格式: 5 字段 "分 时 日 月 周"
    *     匹配任意值
    N     匹配具体值
    N,M   匹配逗号分隔的多个值
    DOM/DOW 使用 OR 语义 (与标准 cron 一致)

持久化: .scheduled_tasks.json — 重启后恢复调度
"""

import json
import queue
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path


# ── Cron 表达式解析 ────────────────────────────────────────────────

def _parse_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """
    解析单个 cron 字段为值集合。

    支持:
        *     → range(min_val, max_val+1)
        N     → {N}
        N,M   → {N, M}
        N-M   → range(N, M+1)
        */N   → range(min_val, max_val+1, N)

    Args:
        field_str: 字段字符串
        min_val: 最小值 (如分钟=0)
        max_val: 最大值 (如分钟=59)

    Returns:
        匹配的整数集合

    Raises:
        ValueError: 字段格式非法
    """
    result = set()
    for part in field_str.split(","):
        part = part.strip()
        if part == "*":
            result.update(range(min_val, max_val + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            result.update(range(min_val, max_val + 1, step))
        elif "-" in part:
            lo, hi = part.split("-", 1)
            result.update(range(int(lo), int(hi) + 1))
        else:
            result.add(int(part))

    # 范围校验
    for v in result:
        if v < min_val or v > max_val:
            raise ValueError(f"Value {v} out of range [{min_val}, {max_val}]")
    return result


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """
    检查 5 字段 cron 表达式是否匹配给定时间。

    字段顺序: 分(0-59) 时(0-23) 日(1-31) 月(1-12) 周(0-6, 0=Sunday)

    DOM/DOW 使用 OR 语义 (与标准 cron 一致):
        如果 DOM 和 DW 都是 *，匹配任意
        如果其中一个是 *，只检查另一个
        如果两个都指定了，满足任一即匹配

    Args:
        cron_expr: 5 字段 cron 表达式，如 "*/5 * * * *"
        dt: 要检查的时间

    Returns:
        True 如果表达式匹配该时间
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Cron expression must have 5 fields, got {len(parts)}: '{cron_expr}'")

    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12)
    dow = _parse_field(parts[4], 0, 6)

    # 基础字段必须匹配
    if dt.minute not in minute:
        return False
    if dt.hour not in hour:
        return False
    if dt.month not in month:
        return False

    # DOM/DOW OR 语义
    dom_match = dt.day in dom
    dow_match = (dt.weekday() + 1) % 7 in dow  # datetime.weekday(): 0=Mon → cron: 0=Sun

    dom_is_wild = parts[2] == "*"
    dow_is_wild = parts[4] == "*"

    if dom_is_wild and dow_is_wild:
        return True
    elif dom_is_wild:
        return dow_match
    elif dow_is_wild:
        return dom_match
    else:
        return dom_match or dow_match


# ── 数据模型 ───────────────────────────────────────────────────────

@dataclass
class ScheduledTask:
    """一个定时任务的配置"""
    cron_id: str                    # cron_{timestamp}_{seq}
    cron_expr: str                  # 5 字段 cron 表达式
    message: str                    # 触发时注入的消息内容
    description: str = ""           # 人类可读描述
    enabled: bool = True            # 是否启用
    created_at: float = field(default_factory=time.time)
    last_triggered: float | None = None  # 上次触发时间


# ── 持久化层 ───────────────────────────────────────────────────────

class CronStore:
    """
    文件持久化的定时任务存储。

    存储位置: .scheduled_tasks.json
    线程安全: 由调用方（CronScheduler._lock）保证
    """

    def __init__(self, workdir: Path):
        self.path = workdir / ".scheduled_tasks.json"
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"cron_{int(time.time())}_{self._seq:04d}"

    def load_all(self) -> list[ScheduledTask]:
        """从文件加载所有定时任务"""
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [ScheduledTask(**item) for item in data]
        except (json.JSONDecodeError, TypeError, KeyError):
            return []

    def save_all(self, tasks: list[ScheduledTask]):
        """将所有定时任务写入文件"""
        data = [asdict(t) for t in tasks]
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── 调度器核心 ─────────────────────────────────────────────────────

class CronScheduler:
    """
    独立守护线程的 Cron 调度器。

    生命周期:
        start() → 启动守护线程，每秒检查一次
        stop()  → 设置停止标志，等待线程退出

    线程安全:
        _lock 保护 _tasks 列表
        _queue 是线程安全的 queue.Queue
    """

    def __init__(self, workdir: Path):
        self._store = CronStore(workdir)
        self._tasks: list[ScheduledTask] = self._store.load_all()
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        """启动调度器守护线程"""
        if self._thread and self._thread.is_alive():
            return  # 已在运行
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """停止调度器守护线程"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self):
        """守护线程主循环: 每秒检查一次 cron 表达式"""
        while not self._stop_event.is_set():
            now = datetime.now()
            with self._lock:
                for task in self._tasks:
                    if not task.enabled:
                        continue
                    # 避免同一秒内重复触发
                    if task.last_triggered and (now.timestamp() - task.last_triggered) < 1:
                        continue
                    try:
                        if cron_matches(task.cron_expr, now):
                            task.last_triggered = now.timestamp()
                            self._queue.put(task.message)
                            print(f"\033[36m[cron] Triggered: {task.cron_id} "
                                  f"({task.description or task.cron_expr})\033[0m")
                    except ValueError as e:
                        print(f"\033[31m[cron] Invalid expression '{task.cron_expr}': {e}\033[0m")
                        task.enabled = False
                # 每次检查后持久化（更新 last_triggered）
                self._store.save_all(self._tasks)
            # 精确到秒级，但不消耗太多 CPU
            self._stop_event.wait(1.0)

    # ── 队列消费 ──────────────────────────────────────────────────

    def collect(self) -> list[str]:
        """
        收集已触发的定时任务消息。

        被 agent loop 每轮开头调用。非阻塞，立即返回所有排队的消息。

        Returns:
            触发消息列表
        """
        messages = []
        while True:
            try:
                msg = self._queue.get_nowait()
                messages.append(msg)
            except queue.Empty:
                break
        return messages

    @property
    def has_pending(self) -> bool:
        """队列中是否有待消费的消息"""
        return not self._queue.empty()

    # ── CRUD 操作 ─────────────────────────────────────────────────

    def add(self, cron_expr: str, message: str, description: str = "") -> ScheduledTask:
        """
        注册新的定时任务。

        Args:
            cron_expr: 5 字段 cron 表达式
            message: 触发时注入的消息
            description: 人类可读描述

        Returns:
            创建的 ScheduledTask

        Raises:
            ValueError: cron 表达式格式非法
        """
        # 验证表达式格式
        cron_matches(cron_expr, datetime.now())

        with self._lock:
            cron_id = self._store._next_id()
            task = ScheduledTask(
                cron_id=cron_id,
                cron_expr=cron_expr,
                message=message,
                description=description,
            )
            self._tasks.append(task)
            self._store.save_all(self._tasks)
        return task

    def list_all(self) -> list[ScheduledTask]:
        """列出所有定时任务"""
        with self._lock:
            return list(self._tasks)

    def get(self, cron_id: str) -> ScheduledTask | None:
        """获取指定定时任务"""
        with self._lock:
            for t in self._tasks:
                if t.cron_id == cron_id:
                    return t
        return None

    def cancel(self, cron_id: str) -> bool:
        """
        取消（删除）定时任务。

        Returns:
            True 如果找到并删除，False 如果不存在
        """
        with self._lock:
            for i, t in enumerate(self._tasks):
                if t.cron_id == cron_id:
                    self._tasks.pop(i)
                    self._store.save_all(self._tasks)
                    return True
        return False

    def enable(self, cron_id: str) -> bool:
        """启用定时任务"""
        with self._lock:
            for t in self._tasks:
                if t.cron_id == cron_id:
                    t.enabled = True
                    self._store.save_all(self._tasks)
                    return True
        return False

    def disable(self, cron_id: str) -> bool:
        """禁用定时任务"""
        with self._lock:
            for t in self._tasks:
                if t.cron_id == cron_id:
                    t.enabled = False
                    self._store.save_all(self._tasks)
                    return True
        return False

    def summary(self) -> str:
        """返回所有定时任务的状态摘要（供工具调用）"""
        with self._lock:
            if not self._tasks:
                return "No scheduled tasks."

            lines = []
            for t in self._tasks:
                icon = "🟢" if t.enabled else "⏸️"
                desc = f" — {t.description}" if t.description else ""
                last = ""
                if t.last_triggered:
                    elapsed = time.time() - t.last_triggered
                    last = f" (last: {_format_elapsed(elapsed)} ago)"
                lines.append(f"  {icon} {t.cron_id} [{t.cron_expr}]{desc}{last}")
                lines.append(f"     message: {t.message[:80]}{'...' if len(t.message) > 80 else ''}")
            return "\n".join(lines)


def _format_elapsed(seconds: float) -> str:
    """格式化时间间隔"""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.0f}m"
    else:
        return f"{seconds / 3600:.1f}h"


# ── 工具 Schema ────────────────────────────────────────────────────

SCHEDULE_CRON_SCHEMA = {
    "name": "schedule_cron",
    "description": (
        "Schedule a recurring task using a cron expression. "
        "The message will be injected into the conversation when triggered. "
        "Cron format: 'minute hour day month weekday' (5 fields). "
        "Examples: '*/5 * * * *' = every 5 min, '0 9 * * 1-5' = weekdays 9am, "
        "'0 0 * * 0' = midnight every Sunday."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "cron_expr": {
                "type": "string",
                "description": "5-field cron expression: 'minute hour day month weekday'.",
            },
            "message": {
                "type": "string",
                "description": "Message to inject when the cron triggers.",
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of what this scheduled task does.",
            },
        },
        "required": ["cron_expr", "message"],
    },
}

LIST_CRONS_SCHEMA = {
    "name": "list_crons",
    "description": "List all scheduled (cron) tasks with their status.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

CANCEL_CRON_SCHEMA = {
    "name": "cancel_cron",
    "description": "Cancel (delete) a scheduled task by its cron ID.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cron_id": {
                "type": "string",
                "description": "The cron task ID to cancel.",
            },
        },
        "required": ["cron_id"],
    },
}


# ── 工具 Handler ───────────────────────────────────────────────────

def make_cron_handlers(scheduler: CronScheduler):
    """返回 3 个 cron 工具的 handler"""

    def run_schedule_cron(cron_expr: str, message: str, description: str = "") -> str:
        try:
            task = scheduler.add(cron_expr, message, description)
            return (
                f"Scheduled task {task.cron_id}: [{cron_expr}]\n"
                f"  Description: {description or '(none)'}\n"
                f"  Message: {message[:100]}{'...' if len(message) > 100 else ''}"
            )
        except ValueError as e:
            return f"Error: invalid cron expression '{cron_expr}': {e}"

    def run_list_crons() -> str:
        return scheduler.summary()

    def run_cancel_cron(cron_id: str) -> str:
        if scheduler.cancel(cron_id):
            return f"Cancelled scheduled task: {cron_id}"
        return f"Scheduled task not found: {cron_id}"

    return {
        "schedule_cron": run_schedule_cron,
        "list_crons": run_list_crons,
        "cancel_cron": run_cancel_cron,
    }
