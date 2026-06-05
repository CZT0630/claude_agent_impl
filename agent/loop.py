"""
Agent 核心循环 — 整个 agent 的心脏

模式:
    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        trigger hooks
        execute tools
        append results
"""

from anthropic import Anthropic
from tools.registry import ToolRegistry
from hooks.manager import HookManager


class AgentLoop:
    def __init__(
        self,
        client: Anthropic,
        model: str,
        system_prompt: str,
        tool_registry: ToolRegistry,
        hook_manager: HookManager | None = None,
        max_tokens: int = 8000,
    ):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tool_registry
        self.hooks = hook_manager or HookManager()
        self.max_tokens = max_tokens

    def run(self, messages: list) -> list:
        while True:
            # 调用 LLM
            response = self.client.messages.create(
                model=self.model,
                system=self.system_prompt,
                messages=messages,
                tools=self.tools.get_definitions(),
                max_tokens=self.max_tokens,
            )
            messages.append({"role": "assistant", "content": response.content})

            # stop_reason: "tool_use"=调工具 / "end_turn"=结束
            if response.stop_reason != "tool_use":
                self.hooks.trigger("Stop", messages)
                return messages

            # 执行工具调用
            results = []
            for block in response.content:
                if block.type == "tool_use" and hasattr(block, "input"):
                    # PreToolUse hook（权限检查在这里）
                    blocked = self.hooks.trigger("PreToolUse", block)
                    if blocked:
                        print(f"\033[31m⛔ {blocked}\033[0m")
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(blocked),
                        })
                        continue

                    # 执行工具
                    output = self.tools.execute(block.name, block.input)

                    # PostToolUse hook
                    self.hooks.trigger("PostToolUse", block, output)

                    print(f"\033[33m> {block.name}\033[0m")
                    try:
                        print(str(output)[:200])
                    except UnicodeEncodeError:
                        print(str(output)[:200].encode("utf-8", errors="replace").decode("utf-8"))

                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    })
            messages.append({"role": "user", "content": results})
