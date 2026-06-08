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
from permissions.pipeline import PermissionPipeline
from hooks.manager import HookManager
from skills.loader import SkillLoader, LOAD_SKILL_SCHEMA, make_load_skill_handler
from context.compact import CompactPipeline
from memory.manager import MemoryManager
from prompt.assembler import PromptAssembler


def build_agent(config: Config) -> AgentLoop:
    client = Anthropic(api_key=config.api_key, base_url=config.base_url)

    # 注册工具
    registry = ToolRegistry()
    registry.register(**BASH_SCHEMA, handler=make_bash_handler(config.workdir))
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


if __name__ == "__main__":
    main()
