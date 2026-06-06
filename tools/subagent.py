"""
Subagent 工具 — 派生子 agent，干净上下文执行子任务

子 agent 用全新 messages[] 执行，完成后只返回摘要，中间结果丢弃。
子 agent 不能调用 task 工具（防止递归派生）。
"""

from anthropic import Anthropic
from agent.loop import AgentLoop
from tools.registry import ToolRegistry


TASK_SCHEMA = {
    "name": "task",
    "description": "Spawn a subagent to handle a subtask. Returns only the final summary.",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "The task for the subagent to complete.",
            },
        },
        "required": ["description"],
    },
}

SUB_SYSTEM_PROMPT = (
    "You are a subagent. Complete the task described by the user, "
    "then return a concise summary of what you did and the result. "
    "Do not use the task tool."
)


def make_subagent_handler(
    client: Anthropic,
    model: str,
    parent_registry: ToolRegistry,
    max_tokens: int = 8000,
):
    """构建 task 工具的 handler，从父注册表复制工具（去掉 task）"""

    # 构建子 agent 工具集：复制父注册表，排除 task 自身
    sub_registry = ToolRegistry()
    for defn, handler in parent_registry.iter_handlers():
        if defn["name"] != "task":
            sub_registry.register(
                name=defn["name"],
                description=defn.get("description", ""),
                input_schema=dict(defn.get("input_schema", {})),
                handler=handler,
            )

    def run_task(description: str) -> str:
        sub_agent = AgentLoop(
            client=client,
            model=model,
            system_prompt=SUB_SYSTEM_PROMPT,
            tool_registry=sub_registry,
            max_tokens=max_tokens,
            max_rounds=30,
        )

        messages = [{"role": "user", "content": description}]
        sub_agent.run(messages)

        # 提取最后一段文本作为摘要
        last: str | list = messages[-1].get("content", "")
        if isinstance(last, str):
            return last
        if isinstance(last, list):
            texts = [getattr(b, "text", "") for b in last if getattr(b, "type", "") == "text"]
            return "\n".join(texts) if texts else "(subagent returned no text)"
        return str(last)

    return run_task
