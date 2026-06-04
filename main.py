#!/usr/bin/env python3
"""
Claude Agent — 入口文件

组装所有模块，启动交互式 CLI。
"""

from anthropic import Anthropic

from agent.config import Config
from agent.loop import AgentLoop
from tools.registry import ToolRegistry
from tools.bash import BASH_SCHEMA, make_bash_handler
from tools.file_ops import (
    READ_SCHEMA, WRITE_SCHEMA, EDIT_SCHEMA, GLOB_SCHEMA,
    make_read_handler, make_write_handler, make_edit_handler, make_glob_handler,
)


def build_agent(config: Config) -> AgentLoop:
    client = Anthropic(api_key=config.api_key, base_url=config.base_url)

    registry = ToolRegistry()
    registry.register(**BASH_SCHEMA, handler=make_bash_handler(config.workdir))
    registry.register(**READ_SCHEMA, handler=make_read_handler(config.workdir))
    registry.register(**WRITE_SCHEMA, handler=make_write_handler(config.workdir))
    registry.register(**EDIT_SCHEMA, handler=make_edit_handler(config.workdir))
    registry.register(**GLOB_SCHEMA, handler=make_glob_handler(config.workdir))

    system_prompt = (
        f"You are a coding agent at {config.workdir}. "
        "Use tools to solve tasks. Act, don't explain."
    )

    return AgentLoop(
        client=client,
        model=config.model,
        system_prompt=system_prompt,
        tool_registry=registry,
        max_tokens=config.max_tokens,
    )


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
