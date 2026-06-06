"""
Agent 核心循环 — 整个 agent 的心脏

模式:
    while stop_reason == "tool_use":
        compact(messages)        ← s08: 四层压缩管线
        response = LLM(messages, tools)
        trigger hooks
        execute tools
        append results
"""

from anthropic import Anthropic
from tools.registry import ToolRegistry
from hooks.manager import HookManager
from context.compact import CompactPipeline
from memory.manager import MemoryManager


class AgentLoop:
    def __init__(
        self,
        client: Anthropic,
        model: str,
        system_prompt: str,
        tool_registry: ToolRegistry,
        hook_manager: HookManager | None = None,
        compact_pipeline: CompactPipeline | None = None,
        memory_manager: MemoryManager | None = None,
        max_tokens: int = 8000,
        max_rounds: int | None = None,
    ):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tool_registry
        self.hooks = hook_manager or HookManager()
        self.compact = compact_pipeline
        self.memory = memory_manager
        self.max_tokens = max_tokens
        self.max_rounds = max_rounds
        self.rounds_since_todo = 0

    def run(self, messages: list) -> list:
        for _ in range(self.max_rounds or 10**9):
            self.rounds_since_todo += 1

            # s08: 四层压缩管线 (budget → snip → micro → auto)
            if self.compact:
                self.compact.run(messages)

            # s09: 选择相关记忆注入上下文（在压缩之后、LLM 调用之前）
            memory_context = ""
            if self.memory:
                memory_context = self.memory.select(messages)
                if memory_context:
                    messages.append({"role": "user", "content": memory_context})

            # UserPromptSubmit hook — nag reminder 等
            injected = self.hooks.trigger("UserPromptSubmit", messages)
            if injected:
                messages.append({"role": "user", "content": injected})

            # 调用 LLM (带 prompt_too_long 紧急恢复)
            try:
                response = self.client.messages.create(
                    model=self.model,
                    system=self.system_prompt,
                    messages=messages,
                    tools=self.tools.get_definitions(),
                    max_tokens=self.max_tokens,
                )
            except Exception as e:
                if "prompt_too_long" in str(e).lower() and self.compact:
                    print(f"\033[33m[compact] prompt_too_long, running reactive compact...\033[0m")
                    self.compact.reactive_compact(messages)
                    response = self.client.messages.create(
                        model=self.model,
                        system=self.system_prompt,
                        messages=messages,
                        tools=self.tools.get_definitions(),
                        max_tokens=self.max_tokens,
                    )
                else:
                    raise

            messages.append({"role": "assistant", "content": response.content})

            # stop_reason: "tool_use"=调工具 / "end_turn"=结束
            if response.stop_reason != "tool_use":
                self.hooks.trigger("Stop", messages)

                # s09: 对话结束时提取新记忆
                if self.memory:
                    new_memories = self.memory.extract(messages)
                    if new_memories:
                        print(f"\033[36m[memory] saved {len(new_memories)} new memory(ies): {', '.join(new_memories)}\033[0m")
                        # 记忆数达到阈值时触发整理
                        remaining = self.memory.consolidate()
                        if remaining < self.memory.memory_count + len(new_memories):
                            print(f"\033[36m[memory] consolidated → {remaining} memories\033[0m")

                return messages

            # 执行工具调用
            results = []
            for block in response.content:
                if block.type == "tool_use" and hasattr(block, "input"):
                    # todo_write 调用时重置 nag 计数器
                    if block.name == "todo_write":
                        self.rounds_since_todo = 0

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

                    # s08: 大输出持久化
                    if self.compact:
                        output = self.compact.truncate_output(str(output), block.id)

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
        return messages
