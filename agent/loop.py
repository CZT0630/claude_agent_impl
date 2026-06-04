"""
Agent 核心循环 — 整个 agent 的心脏

模式:
    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        check permission    ← s03 新增
        execute tools
        append results
"""

from anthropic import Anthropic
from tools.registry import ToolRegistry
from permissions.pipeline import PermissionPipeline


class AgentLoop:
    def __init__(
        self,
        client: Anthropic,
        model: str,
        system_prompt: str,
        tool_registry: ToolRegistry,
        permission_pipeline: PermissionPipeline | None = None,
        max_tokens: int = 8000,
    ):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tool_registry
        self.permissions = permission_pipeline
        self.max_tokens = max_tokens

    def run(self, messages: list) -> list:
        while True:
            response = self.client.messages.create(
                model=self.model,
                system=self.system_prompt,
                messages=messages,
                tools=self.tools.get_definitions(),
                max_tokens=self.max_tokens,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                return messages

            results = []
            for block in response.content:
                if block.type == "tool_use" and hasattr(block, "input"):
                    # s03: 权限检查，被拒绝则跳过执行
                    if self.permissions:
                        denied = self.permissions.check(block.name, block.input)
                        if denied:
                            print(f"\033[31m⛔ {denied}\033[0m")
                            results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": denied,
                            })
                            continue

                    output = self.tools.execute(block.name, block.input)

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
