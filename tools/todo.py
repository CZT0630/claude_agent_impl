"""
TodoWrite 工具 — 任务计划管理

让 agent 先列计划再动手，状态保存在内存中。
"""


TODO_SCHEMA = {
    "name": "todo_write",
    "description": "Create and manage a task list. Call this to plan before executing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Task description"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "Task status",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
}


def make_todo_handler():
    """返回 todo_write 工具的 handler"""

    def run_todo_write(todos: list[dict]) -> str:
        # 验证 status 枚举值
        valid_statuses = {"pending", "in_progress", "completed"}
        for item in todos:
            if item.get("status") not in valid_statuses:
                return f"Error: invalid status '{item.get('status')}', must be one of {valid_statuses}"

        # 格式化输出
        lines = []
        status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}
        for item in todos:
            icon = status_icon.get(item["status"], "?")
            lines.append(f"  {icon} [{item['status']}] {item['content']}")

        summary = f"Todo list updated ({len(todos)} items):\n" + "\n".join(lines)
        return summary

    return run_todo_write
