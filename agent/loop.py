"""
Agent 核心循环 — 整个 agent 的心脏

模式:
    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results
"""

from anthropic import Anthropic
from tools.registry import ToolRegistry


class AgentLoop:
    def __init__(
        self,
        client: Anthropic,
        model: str,
        system_prompt: str,
        tool_registry: ToolRegistry,
        max_tokens: int = 8000,
    ):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tool_registry
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
