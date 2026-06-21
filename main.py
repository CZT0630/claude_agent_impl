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
from agent.event_store import JsonlEventStore
from agent.loop import AgentLoop
from agent.runtime import AgentRuntime
from agent.state import LoopState
from tools.registry import ToolRegistry
from tools.bash import BASH_SCHEMA, make_bash_handler
from tools.file_ops import (
    EDIT_SCHEMA,
    GLOB_SCHEMA,
    READ_SCHEMA,
    WRITE_SCHEMA,
    make_edit_handler,
    make_glob_handler,
    make_read_handler,
    make_write_handler,
)
from tools.todo import TODO_SCHEMA, make_todo_handler
from tools.subagent import TASK_SCHEMA, make_subagent_handler
from tools.task import (
    CLAIM_TASK_SCHEMA,
    COMPLETE_TASK_SCHEMA,
    CREATE_TASK_SCHEMA,
    GET_TASK_SCHEMA,
    LIST_TASKS_SCHEMA,
    TaskStore,
    make_task_handlers,
)
from tools.background import BackgroundManager, CHECK_BACKGROUND_SCHEMA  # s13: 后台任务
from tools.cron import (
    CANCEL_CRON_SCHEMA,
    LIST_CRONS_SCHEMA,
    SCHEDULE_CRON_SCHEMA,
    CronScheduler,
    make_cron_handlers,
)
from tools.teams import (  # s15: Agent Teams
    CHECK_INBOX_SCHEMA,
    SEND_MESSAGE_SCHEMA,
    SPAWN_TEAMMATE_SCHEMA,
    TEAM_STATUS_SCHEMA,
    TeamManager,
    make_check_inbox_handler, make_team_status_handler,
    make_send_message_handler,
    make_spawn_teammate_handler,
)
from tools.team_protocols import (  # s16: Team Protocols
    PROTOCOL_STATUS_SCHEMA,
    REQUEST_PLAN_SCHEMA,
    REQUEST_SHUTDOWN_SCHEMA,
    REVIEW_PLAN_SCHEMA,
    ProtocolManager,
    make_protocol_status_handler,
    make_request_shutdown_handler, make_request_plan_handler,
    make_review_plan_handler,
)
from tools.worktree import (  # s18: Worktree Isolation
    CREATE_WORKTREE_SCHEMA,
    KEEP_WORKTREE_SCHEMA,
    REMOVE_WORKTREE_SCHEMA,
    WorktreeManager,
    make_worktree_handlers,
)
from tools.mcp import (  # s19: MCP Plugin
    MCP_CONNECT_SCHEMA,
    MCP_DISCONNECT_SCHEMA,
    MCP_LIST_TOOLS_SCHEMA,
    MCPManager,
    make_mcp_handlers,
)
from permissions.pipeline import PermissionPipeline
from hooks.manager import HookManager
from skills.loader import SkillLoader, LOAD_SKILL_SCHEMA, make_load_skill_handler
from context.compact import CompactPipeline
from memory.manager import MemoryManager
from prompt.assembler import PromptAssembler


def _register_basic_tools(
    registry: ToolRegistry,
    config: Config,
    bg_manager: BackgroundManager,
) -> None:
    """Register tools that do not depend on higher-level runtime services."""
    registry.register(
        **BASH_SCHEMA,
        handler=make_bash_handler(
            config.workdir,
            bg_manager,
            config.sandbox_level,
        ),
    )
    registry.register(**READ_SCHEMA, handler=make_read_handler(config.workdir))
    registry.register(**WRITE_SCHEMA, handler=make_write_handler(config.workdir))
    registry.register(**EDIT_SCHEMA, handler=make_edit_handler(config.workdir))
    registry.register(**GLOB_SCHEMA, handler=make_glob_handler(config.workdir))
    registry.register(**TODO_SCHEMA, handler=make_todo_handler())


def _register_subagent_tool(
    registry: ToolRegistry,
    client: Anthropic,
    config: Config,
) -> None:
    registry.register(
        **TASK_SCHEMA,
        handler=make_subagent_handler(
            client=client,
            model=config.model,
            parent_registry=registry,
            max_tokens=config.max_tokens,
        ),
    )


def _register_task_tools(registry: ToolRegistry, config: Config) -> TaskStore:
    task_store = TaskStore(config.workdir / ".tasks")
    handlers = make_task_handlers(task_store)
    registry.register(**CREATE_TASK_SCHEMA, handler=handlers["create_task"])
    registry.register(**LIST_TASKS_SCHEMA, handler=handlers["list_tasks"])
    registry.register(**GET_TASK_SCHEMA, handler=handlers["get_task"])
    registry.register(**CLAIM_TASK_SCHEMA, handler=handlers["claim_task"])
    registry.register(**COMPLETE_TASK_SCHEMA, handler=handlers["complete_task"])
    return task_store


def _register_skill_tool(registry: ToolRegistry) -> SkillLoader:
    skill_loader = SkillLoader()
    registry.register(**LOAD_SKILL_SCHEMA, handler=make_load_skill_handler(skill_loader))
    return skill_loader


def _register_background_tool(
    registry: ToolRegistry,
    bg_manager: BackgroundManager,
) -> None:
    registry.register(**CHECK_BACKGROUND_SCHEMA, handler=lambda: bg_manager.status())


def _register_cron_tools(registry: ToolRegistry, config: Config) -> CronScheduler:
    scheduler = CronScheduler(config.workdir)
    handlers = make_cron_handlers(scheduler)
    registry.register(**SCHEDULE_CRON_SCHEMA, handler=handlers["schedule_cron"])
    registry.register(**LIST_CRONS_SCHEMA, handler=handlers["list_crons"])
    registry.register(**CANCEL_CRON_SCHEMA, handler=handlers["cancel_cron"])
    return scheduler


def _register_worktree_tools(registry: ToolRegistry, config: Config) -> WorktreeManager:
    manager = WorktreeManager(config.workdir)
    handlers = make_worktree_handlers(manager)
    registry.register(**CREATE_WORKTREE_SCHEMA, handler=handlers["create_worktree"])
    registry.register(**REMOVE_WORKTREE_SCHEMA, handler=handlers["remove_worktree"])
    registry.register(**KEEP_WORKTREE_SCHEMA, handler=handlers["keep_worktree"])
    return manager


def _register_mcp_tools(registry: ToolRegistry, config: Config) -> MCPManager:
    manager = MCPManager(config.workdir)
    handlers = make_mcp_handlers(manager, registry)
    registry.register(**MCP_CONNECT_SCHEMA, handler=handlers["mcp_connect"])
    registry.register(**MCP_DISCONNECT_SCHEMA, handler=handlers["mcp_disconnect"])
    registry.register(**MCP_LIST_TOOLS_SCHEMA, handler=handlers["mcp_list_tools"])
    return manager


def _register_team_tools(
    registry: ToolRegistry,
    client: Anthropic,
    config: Config,
    task_store: TaskStore,
    worktree_manager: WorktreeManager,
) -> TeamManager:
    team_manager = TeamManager(
        client=client,
        model=config.model,
        workdir=config.workdir,
        parent_registry=registry,
        task_store=task_store,
        worktree_manager=worktree_manager,
        max_tokens=config.max_tokens,
    )
    registry.register(
        **SEND_MESSAGE_SCHEMA,
        handler=make_send_message_handler(team_manager.bus, "lead"),
    )
    registry.register(
        **CHECK_INBOX_SCHEMA,
        handler=make_check_inbox_handler(team_manager.bus, "lead"),
    )
    registry.register(
        **SPAWN_TEAMMATE_SCHEMA,
        handler=make_spawn_teammate_handler(team_manager),
    )
    registry.register(**TEAM_STATUS_SCHEMA, handler=make_team_status_handler(team_manager))
    return team_manager


def _register_protocol_tools(
    registry: ToolRegistry,
    team_manager: TeamManager,
) -> None:
    protocol_manager = ProtocolManager(team_manager.bus)
    registry.register(
        **REQUEST_SHUTDOWN_SCHEMA,
        handler=make_request_shutdown_handler(protocol_manager, team_manager),
    )
    registry.register(
        **REQUEST_PLAN_SCHEMA,
        handler=make_request_plan_handler(protocol_manager, team_manager),
    )
    registry.register(
        **REVIEW_PLAN_SCHEMA,
        handler=make_review_plan_handler(protocol_manager, team_manager.bus),
    )
    registry.register(
        **PROTOCOL_STATUS_SCHEMA,
        handler=make_protocol_status_handler(protocol_manager),
    )


def _build_prompt_assembler(config: Config, skill_loader: SkillLoader) -> PromptAssembler:
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
            "Use spawn_teammate(worktree='name') to isolate a teammate in its own git worktree. "
            "Use create_worktree/remove_worktree/keep_worktree to manage worktrees directly. "
            "Communicate via send_message/check_inbox. "
            "Use team_status to monitor teammates. "
            "Use request_shutdown to gracefully stop a teammate. "
            "Use request_plan to ask a teammate for a plan before they start. "
            "Use review_plan to approve/reject a teammate's plan. "
            "Use protocol_status to track protocol requests. "
            "Teammates will automatically claim unclaimed tasks from the task board "
            "when idle. No need to manually assign tasks - just create them with "
            "create_task and teammates will pick them up automatically. "
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
    assembler.register(
        "mcp",
        lambda ctx: (
            "MCP (Model Context Protocol) servers provide external tools. "
            "Use mcp_connect(server_name) to connect and discover tools, "
            "mcp_disconnect(server_name) to disconnect, "
            "mcp_list_tools to see all available MCP tools. "
            "MCP tools are named mcp__{server}__{tool} and can be called directly."
        ),
        condition=lambda ctx: True,
        priority=25,
    )
    return assembler


def _build_tool_hooks(config: Config) -> HookManager:
    hooks = HookManager()
    permissions = PermissionPipeline(config.workdir)

    def permission_hook(block):
        return permissions.check(block.name, block.input)

    def log_hook(block):
        print(f"\033[90m[hook] {block.name}\033[0m")
        return None

    def large_output_hook(block, output):
        if len(str(output)) > 10000:
            print(
                f"\033[33m[hook] ⚠ Large output from {block.name}: "
                f"{len(str(output))} chars\033[0m"
            )
        return None

    hooks.register("PreToolUse", permission_hook)
    hooks.register("PreToolUse", log_hook)
    hooks.register("PostToolUse", large_output_hook)
    return hooks


def _register_loop_hooks(hooks: HookManager, agent: AgentLoop) -> None:
    def nag_reminder_hook(_messages):
        if agent.rounds_since_todo >= 3:
            agent.rounds_since_todo = 0
            return (
                "<reminder>You haven't updated your todo list recently. "
                "Call todo_write to update your plan.</reminder>"
            )
        return None

    hooks.register("UserPromptSubmit", nag_reminder_hook)


def build_agent(config: Config) -> AgentLoop:
    """Assemble the CLI agent from runtime services, tools, hooks, and prompt."""
    client = Anthropic(api_key=config.api_key, base_url=config.base_url)
    registry = ToolRegistry()

    bg_manager = BackgroundManager(config.workdir)
    _register_basic_tools(registry, config, bg_manager)
    _register_subagent_tool(registry, client, config)
    task_store = _register_task_tools(registry, config)
    skill_loader = _register_skill_tool(registry)
    _register_background_tool(registry, bg_manager)
    cron_scheduler = _register_cron_tools(registry, config)
    worktree_manager = _register_worktree_tools(registry, config)
    mcp_manager = _register_mcp_tools(registry, config)
    team_manager = _register_team_tools(
        registry,
        client,
        config,
        task_store,
        worktree_manager,
    )
    _register_protocol_tools(registry, team_manager)

    assembler = _build_prompt_assembler(config, skill_loader)
    hooks = _build_tool_hooks(config)
    compact_pipeline = CompactPipeline(
        client=client,
        model=config.model,
        workdir=config.workdir,
    )
    memory_manager = MemoryManager(
        client=client,
        model=config.model,
        workdir=config.workdir,
    )

    agent = AgentLoop(
        client=client,
        model=config.model,
        system_prompt=assembler,
        tool_registry=registry,
        hook_manager=hooks,
        compact_pipeline=compact_pipeline,
        memory_manager=memory_manager,
        bg_manager=bg_manager,
        cron_scheduler=cron_scheduler,
        team_manager=team_manager,
        mcp_manager=mcp_manager,
        max_tokens=config.max_tokens,
        fallback_model=config.fallback_model,
    )
    _register_loop_hooks(hooks, agent)
    return agent


def create_cli_state(
    query: str,
    *,
    config: Config,
    history: list,
    session_id: str,
) -> LoopState:
    """Create one turn state while keeping the CLI session id stable."""
    return LoopState.from_user_message(
        query,
        model=config.model,
        workdir=config.workdir,
        session_id=session_id,
        messages=history,
    )


def _start_background_services(agent: AgentLoop) -> None:
    if agent.cron:
        agent.cron.start()

    if agent.mcp_manager:
        mcp_result = agent.mcp_manager.auto_connect_all()
        if mcp_result and "No MCP" not in mcp_result:
            print(f"\033[36m[mcp] {mcp_result}\033[0m")


def _stop_background_services(agent: AgentLoop) -> None:
    if agent.cron:
        agent.cron.stop()

    if agent.mcp_manager:
        agent.mcp_manager.shutdown_all()


def _print_banner(config: Config) -> None:
    print(f"Claude Agent — model: {config.model}")
    print(f"Working directory: {config.workdir}")
    print("输入问题, 回车发送。输入 q 退出。\n")


def _print_last_assistant_message(history: list) -> None:
    last = history[-1].get("content", [])
    if isinstance(last, list):
        for block in last:
            if getattr(block, "type", None) == "text" and hasattr(block, "text"):
                try:
                    print(block.text)
                except UnicodeEncodeError:
                    print(
                        block.text.encode("utf-8", errors="replace").decode("utf-8")
                    )
    print()


def main():
    config = Config.from_env()
    agent = build_agent(config)
    runtime = AgentRuntime(
        agent,
        event_store=JsonlEventStore.for_workdir(config.workdir),
    )
    _start_background_services(agent)
    _print_banner(config)

    try:
        history = []
        session_id = LoopState.new_session_id()
        while True:
            try:
                query = input("\033[36m>> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break

            history.append({"role": "user", "content": query})
            state = create_cli_state(
                query,
                config=config,
                history=history,
                session_id=session_id,
            )
            runtime.execute(state)
            history = state.messages
            _print_last_assistant_message(history)
    finally:
        _stop_background_services(agent)


if __name__ == "__main__":
    main()
