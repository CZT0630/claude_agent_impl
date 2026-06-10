"""
Autonomous Agents — s17: 自己看板认领活

问题: s15/s16 的队友需要 Lead 逐个分配任务，不够自主。
方案: 队友在 IDLE 阶段自动扫描任务看板，认领未分配的任务。

生命周期:
    WORK: inbox → LLM → tools → (tool_use? loop) → (done? → IDLE)
    IDLE: 5s poll → inbox? → WORK / unclaimed? → claim → WORK / 60s? → SHUTDOWN

自动认领逻辑:
    1. 每 5 秒扫描一次任务看板
    2. 查找 status=pending, owner=None, 依赖已完成 的任务
    3. 自动认领并进入 WORK 阶段
    4. 如果 60 秒没有找到可认领的任务，自动关机

新增队友工具:
    list_tasks   — 查看任务列表
    claim_task   — 认领任务
    complete_task — 完成任务
"""

import threading
import time
from pathlib import Path

from tools.task import TaskStore, can_start


# ── 自动认领逻辑 ─────────────────────────────────────────────────────

def scan_unclaimed_tasks(store: TaskStore) -> list:
    """
    扫描未认领的、依赖已完成的 pending 任务。

    Returns:
        可认领的任务列表
    """
    tasks = store.list_all()
    return [
        t for t in tasks
        if t.status == "pending"
        and t.owner is None
        and can_start(t, store)
    ]


def auto_claim_task(store: TaskStore, agent_name: str) -> str | None:
    """
    自动认领一个可执行的任务。

    Args:
        store: 任务存储
        agent_name: 当前 agent 名字

    Returns:
        认领的任务 ID，或 None（没有可认领的任务）
    """
    unclaimed = scan_unclaimed_tasks(store)
    if not unclaimed:
        return None

    # 认领第一个可用任务
    task = unclaimed[0]
    task.status = "in_progress"
    task.owner = agent_name
    store.save(task)
    return task.id


# ── 队友生命周期增强 ─────────────────────────────────────────────────

class AutonomousTeammateLifecycle:
    """
    自主队友的生命周期管理器。

    在原有 WORK/IDLE 基础上增加:
    - IDLE 阶段自动扫描任务看板
    - 自动认领可执行的任务
    - 无任务时的优雅关机
    """

    def __init__(
        self,
        store: TaskStore,
        agent_name: str,
        bus,
        idle_poll_interval: float = 5.0,
        idle_shutdown_timeout: float = 60.0,
    ):
        self.store = store
        self.agent_name = agent_name
        self.bus = bus
        self.idle_poll_interval = idle_poll_interval
        self.idle_shutdown_timeout = idle_shutdown_timeout

    def idle_loop_with_auto_claim(self, on_task_claimed, on_shutdown):
        """
        IDLE 循环，支持自动认领任务。

        Args:
            on_task_claimed: 认领任务后的回调 (task_id, task_subject)
            on_shutdown: 关机时的回调
        """
        idle_start = time.time()

        while True:
            # 1. 检查邮箱是否有新消息
            inbox = self.bus.receive(self.agent_name)
            if inbox:
                idle_start = time.time()  # 重置空闲计时
                # 处理消息后返回 WORK 状态
                return "work", inbox

            # 2. 扫描任务看板，尝试自动认领
            task_id = auto_claim_task(self.store, self.agent_name)
            if task_id:
                task = self.store.load(task_id)
                idle_start = time.time()  # 重置空闲计时
                on_task_claimed(task_id, task.subject if task else "unknown")
                return "work_claimed", task_id

            # 3. 空闲超时检查
            if time.time() - idle_start > self.idle_shutdown_timeout:
                on_shutdown()
                return "shutdown", None

            # 4. 等待下一次轮询
            time.sleep(self.idle_poll_interval)


# ── 工具 Schema ──────────────────────────────────────────────────────

# 注意: list_tasks, claim_task, complete_task 已经在 tools/task.py 中定义
# 这里只需要确保队友的工具集包含这些工具

AUTONOMOUS_SYSTEM_PROMPT_ADDITIONS = """
You are an autonomous teammate agent. Your workflow:

1. When you receive a task, work on it and complete it.
2. After completing a task, use complete_task to mark it done.
3. When idle, you will automatically claim unclaimed tasks from the task board.
4. If no tasks are available for 60 seconds, you will shut down automatically.

Available task management tools:
- list_tasks: See all tasks on the board
- claim_task: Claim a specific task (auto-claim will do this for you)
- complete_task: Mark a task as completed

You can also:
- Use send_message to communicate with the lead agent
- Use check_inbox to receive messages

Work autonomously. Don't wait for instructions - claim tasks proactively.
"""


# ── 集成说明 ─────────────────────────────────────────────────────────

"""
集成到 TeamManager 的修改点:

1. 在 _build_teammate_registry 中添加 list_tasks, claim_task, complete_task 工具
2. 在 spawn 方法的 IDLE 阶段使用 AutonomousTeammateLifecycle
3. 更新 TEAMMATE_SYSTEM prompt，添加自主工作指令

示例修改 (在 tools/teams.py 中):

```python
def _build_teammate_registry(self, teammate_name: str) -> ToolRegistry:
    excluded = {"task", "spawn_teammate"}
    registry = ToolRegistry()

    for defn, handler in self.parent_registry.iter_handlers():
        if defn["name"] not in excluded:
            registry.register(...)

    # 添加任务管理工具给队友
    from tools.task import make_task_handlers
    task_handlers = make_task_handlers(self.task_store)
    registry.register(name="list_tasks", ..., handler=task_handlers["list_tasks"])
    registry.register(name="claim_task", ..., handler=task_handlers["claim_task"])
    registry.register(name="complete_task", ..., handler=task_handlers["complete_task"])

    # 添加消息工具
    registry.register(...)
    return registry
```

```python
# 在 spawn 方法的 IDLE 阶段
from s17_autonomous_agents.code import AutonomousTeammateLifecycle

lifecycle = AutonomousTeammateLifecycle(
    store=self.task_store,
    agent_name=name,
    bus=self.bus,
)

while True:
    result, data = lifecycle.idle_loop_with_auto_claim(
        on_task_claimed=lambda tid, subj: print(f"[{name}] Auto-claimed task: {subj}"),
        on_shutdown=lambda: print(f"[{name}] Shutting down (no tasks available)"),
    )

    if result == "shutdown":
        break
    elif result == "work_claimed":
        # 执行认领的任务
        task = self.task_store.load(data)
        # ... 创建 agent loop 执行任务 ...
    elif result == "work":
        # 处理收到的消息
        # ... 处理 inbox 消息 ...
```
"""
