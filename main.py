#!/usr/bin/env python3
"""
Claude Agent — 入口文件

组装所有模块，启动交互式 CLI。
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from anthropic import Anthropic

from agent.config import Config
from agent.loop import AgentLoop
from tools.registry import ToolRegistry
from tools.bash import BASH_SCHEMA, make_bash_handler
from tools.file_ops import (
    READ_SCHEMA, WRITE_SCHEMA, EDIT_SCHEMA, GLOB_SCHEMA,
    make_read_handler, make_write_handler, make_edit_handler, make_glob_handler,
)
from tools.todo import TODO_SCHEMA, make_todo_handler
from tools.subagent import TASK_SCHEMA, make_subagent_handler
from tools.task import (
    CREATE_TASK_SCHEMA, LIST_TASKS_SCHEMA, GET_TASK_SCHEMA,
    CLAIM_TASK_SCHEMA, COMPLETE_TASK_SCHEMA,
    TaskStore, make_task_handlers,
)
from tools.background import BackgroundManager, CHECK_BACKGROUND_SCHEMA  # s13: 后台任务
from tools.cron import (
    CronScheduler, SCHEDULE_CRON_SCHEMA, LIST_CRONS_SCHEMA, CANCEL_CRON_SCHEMA,
    make_cron_handlers,
)
from tools.teams import (  # s15: Agent Teams
    TeamManager, MessageBus,
    SPAWN_TEAMMATE_SCHEMA, SEND_MESSAGE_SCHEMA, CHECK_INBOX_SCHEMA, TEAM_STATUS_SCHEMA,
    make_spawn_teammate_handler, make_send_message_handler,
    make_check_inbox_handler, make_team_status_handler,
)
from tools.team_protocols import (  # s16: Team Protocols
    ProtocolManager,
    REQUEST_SHUTDOWN_SCHEMA, REQUEST_PLAN_SCHEMA, REVIEW_PLAN_SCHEMA, PROTOCOL_STATUS_SCHEMA,
    make_request_shutdown_handler, make_request_plan_handler,
    make_review_plan_handler, make_protocol_status_handler,
)
from permissions.pipeline import PermissionPipeline
from hooks.manager import HookManager
from skills.loader import SkillLoader, LOAD_SKILL_SCHEMA, make_load_skill_handler
from context.compact import CompactPipeline
from memory.manager import MemoryManager
from prompt.assembler import PromptAssembler


def build_agent(config: Config) -> AgentLoop:
    client = Anthropic(api_key=config.api_key, base_url=config.base_url)

    # s13: 后台任务管理器 — 管理所有后台执行的 shell 命令
    bg_manager = BackgroundManager(config.workdir)

    # 注册工具
    registry = ToolRegistry()
    # s13: bash handler 接收 bg_manager，支持 run_in_background 参数
    registry.register(**BASH_SCHEMA, handler=make_bash_handler(config.workdir, bg_manager))
    registry.register(**READ_SCHEMA, handler=make_read_handler(config.workdir))
    registry.register(**WRITE_SCHEMA, handler=make_write_handler(config.workdir))
    registry.register(**EDIT_SCHEMA, handler=make_edit_handler(config.workdir))
    registry.register(**GLOB_SCHEMA, handler=make_glob_handler(config.workdir))
    registry.register(**TODO_SCHEMA, handler=make_todo_handler())
    registry.register(**TASK_SCHEMA, handler=make_subagent_handler(client=client,model=config.model,parent_registry=registry,
max_tokens=config.max_tokens,
    ))
    # s12: 任务系统（文件持久化的任务图，支持依赖关系）
    task_store = TaskStore(config.workdir / ".tasks")
    task_handlers = make_task_handlers(task_store)
    registry.register(**CREATE_TASK_SCHEMA, handler=task_handlers["create_task"])
    registry.register(**LIST_TASKS_SCHEMA, handler=task_handlers["list_tasks"])
    registry.register(**GET_TASK_SCHEMA, handler=task_handlers["get_task"])
    registry.register(**CLAIM_TASK_SCHEMA, handler=task_handlers["claim_task"])
    registry.register(**COMPLETE_TASK_SCHEMA, handler=task_handlers["complete_task"])

    # s07: 技能加载（两级按需注入）
    skill_loader = SkillLoader()
    registry.register(**LOAD_SKILL_SCHEMA, handler=make_load_skill_handler(skill_loader))

    # s13: check_background 工具 — 手动查询后台任务状态
    # 通常不需要主动调用，agent loop 每轮会自动 collect()
    registry.register(**CHECK_BACKGROUND_SCHEMA, handler=lambda: bg_manager.status())

    # s14: Cron 定时调度器 — 独立守护线程按时触发任务
    cron_scheduler = CronScheduler(config.workdir)
    cron_handlers = make_cron_handlers(cron_scheduler)
    registry.register(**SCHEDULE_CRON_SCHEMA, handler=cron_handlers["schedule_cron"])
    registry.register(**LIST_CRONS_SCHEMA, handler=cron_handlers["list_crons"])
    registry.register(**CANCEL_CRON_SCHEMA, handler=cron_handlers["cancel_cron"])

    # s15: Agent Teams — 消息总线 + 文件邮箱 + 异步队友
    team_manager = TeamManager(
        client=client,
        model=config.model,
        workdir=config.workdir,
        parent_registry=registry,
        max_tokens=config.max_tokens,
    )
    # Lead agent 的 send_message / check_inbox（sender_name="lead"）
    registry.register(**SEND_MESSAGE_SCHEMA, handler=make_send_message_handler(team_manager.bus, "lead"))
    registry.register(**CHECK_INBOX_SCHEMA, handler=make_check_inbox_handler(team_manager.bus, "lead"))
    # spawn_teammate / team_status 只有 lead 能用
    registry.register(**SPAWN_TEAMMATE_SCHEMA, handler=make_spawn_teammate_handler(team_manager))
    registry.register(**TEAM_STATUS_SCHEMA, handler=make_team_status_handler(team_manager))

    # s16: Team Protocols — 请求-回复协议 + 状态机
    protocol_mgr = ProtocolManager(team_manager.bus)
    registry.register(**REQUEST_SHUTDOWN_SCHEMA, handler=make_request_shutdown_handler(protocol_mgr, team_manager))
    registry.register(**REQUEST_PLAN_SCHEMA, handler=make_request_plan_handler(protocol_mgr, team_manager))
    registry.register(**REVIEW_PLAN_SCHEMA, handler=make_review_plan_handler(protocol_mgr, team_manager.bus))
    registry.register(**PROTOCOL_STATUS_SCHEMA, handler=make_protocol_status_handler(protocol_mgr))

    # s10: 动态 Prompt 组装 — 段落注册表 + 条件加载
    assembler = PromptAssembler()
    assembler.register(
        "identity",
        f"You are a coding agent at {config.workdir}.",
        priority=0,
    )
    assembler.register(
        "behavior",
        (
            "Use tools to solve tasks. Plan before execute: "
            "call todo_write to create a task list first, "
            "then update it as you progress. "
            "For persistent tasks, use create_task/list_tasks/complete_task "
            "to manage a task board that survives across sessions. "
            "For complex subtasks, use the task tool to spawn a subagent. "
            "For long-running commands, set run_in_background=true in bash "
            "and use check_background to poll for results. "
            "For recurring tasks, use schedule_cron with a 5-field cron expression "
            "(minute hour day month weekday) to auto-trigger messages. "
            "Use list_crons to see scheduled tasks, cancel_cron to remove them. "
            "For parallel teamwork, use spawn_teammate to launch agents on subtasks. "
            "Communicate via send_message/check_inbox. "
            "Use team_status to monitor teammates. "
            "Use request_shutdown to gracefully stop a teammate. "
            "Use request_plan to ask a teammate for a plan before they start. "
            "Use review_plan to approve/reject a teammate's plan. "
            "Use protocol_status to track protocol requests. "
            "Act, don't explain."
        ),
        priority=10,
    )
    assembler.register(
        "skills",
        lambda ctx: (
            f"Skills available:\n{skill_loader.list_skills()}\n"
            "Use load_skill(name) to get full details."
        ),
        condition=lambda ctx: skill_loader.has_skills,
        priority=20,
    )
    assembler.register(
        "memory",
        lambda ctx: (
            "Relevant memories are injected below when available. "
            "Use them to personalize your behavior."
        ),
        condition=lambda ctx: ctx.get("has_memories", False),
        priority=30,
    )

    # s08: 四层上下文压缩管线
    compact_pipeline = CompactPipeline(
        client=client,
        model=config.model,
        workdir=config.workdir,
    )

    # s09: 跨会话持久记忆 (~/.claude/projects/{project_key}/memory/)
    memory_manager = MemoryManager(
        client=client,
        model=config.model,
        workdir=config.workdir,
    )

    # 配置 hooks
    hooks = HookManager()
    permissions = PermissionPipeline(config.workdir)

    # PreToolUse: 权限检查
    def permission_hook(block):
        return permissions.check(block.name, block.input)

    # PreToolUse: 日志记录
    def log_hook(block):
        print(f"\033[90m[hook] {block.name}\033[0m")
        return None

    # PostToolUse: 大输出警告
    def large_output_hook(block, output):
        if len(str(output)) > 10000:
            print(f"\033[33m[hook] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
        return None

    hooks.register("PreToolUse", permission_hook)
    hooks.register("PreToolUse", log_hook)
    hooks.register("PostToolUse", large_output_hook)

    agent = AgentLoop(
        client=client,
        model=config.model,
        system_prompt=assembler,
        tool_registry=registry,
        hook_manager=hooks,
        compact_pipeline=compact_pipeline,
        memory_manager=memory_manager,
        bg_manager=bg_manager,    # s13: agent loop 每轮自动收集后台结果
        cron_scheduler=cron_scheduler,  # s14: agent loop 每轮自动收集定时触发
        team_manager=team_manager,      # s15: agent loop 每轮自动收集队友消息
        max_tokens=config.max_tokens,
        fallback_model=config.fallback_model,
    )

    # UserPromptSubmit: nag reminder — 3 轮没更新 todo 就提醒
    def nag_reminder_hook(_messages):
        if agent.rounds_since_todo >= 3:
            agent.rounds_since_todo = 0  # 重置，避免连续提醒
            return "<reminder>You haven't updated your todo list recently. Call todo_write to update your plan.</reminder>"
        return None

    hooks.register("UserPromptSubmit", nag_reminder_hook)

    return agent


def main():
    config = Config.from_env()
    agent = build_agent(config)

    # s14: 启动 cron 调度器守护线程
    if agent.cron:
        agent.cron.start()

    print(f"Claude Agent — model: {config.model}")
    print(f"Working directory: {config.workdir}")
    print("输入问题, 回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36m>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent.run(history)

        last = history[-1].get("content", [])
        if isinstance(last, list):
            for block in last:
                if getattr(block, "type", None) == "text" and hasattr(block, "text"):
                    try:
                        print(block.text)
                    except UnicodeEncodeError:
                        print(block.text.encode("utf-8", errors="replace").decode("utf-8"))
        print()

    # s14: 停止 cron 调度器（干净退出）
    if agent.cron:
        agent.cron.stop()


if __name__ == "__main__":
    main()
