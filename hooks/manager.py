"""
Hook 管理器 — 在循环的关键节点插入扩展逻辑

事件:
    UserPromptSubmit: 用户输入后、LLM 调用前
    PreToolUse:       工具执行前（权限检查在这里）
    PostToolUse:      工具执行后（日志、大输出警告）
    Stop:             循环即将退出时

设计原则: 挂在循环上，不写进循环里
"""


class HookManager:
    def __init__(self):
        self._hooks: dict[str, list] = {
            "UserPromptSubmit": [],
            "PreToolUse": [],
            "PostToolUse": [],
            "Stop": [],
        }

    def register(self, event: str, callback):
        """注册一个 hook 到指定事件"""
        if event not in self._hooks:
            raise ValueError(f"Unknown event: {event}")
        self._hooks[event].append(callback)

    def trigger(self, event: str, *args):
        """
        触发事件的所有 hooks。
        返回 None 表示继续，返回非 None 表示阻断。
        """
        for callback in self._hooks.get(event, []):
            result = callback(*args)
            if result is not None:
                return result
        return None
