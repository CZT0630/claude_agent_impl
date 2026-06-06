"""
工具注册表 — 管理工具定义和分发

新工具 = 新函数 + 新 schema + 注册到 registry
"""

from typing import Callable
from anthropic.types import ToolParam


class ToolRegistry:
    def __init__(self):
        self._handlers: dict[str, Callable] = {}
        self._definitions: list[ToolParam] = []

    def register(self, name: str, description: str, input_schema: dict, handler: Callable):
        self._handlers[name] = handler
        self._definitions.append({
            "name": name,
            "description": description,
            "input_schema": input_schema,
        })

    def get_definitions(self) -> list[ToolParam]:
        return self._definitions

    def iter_handlers(self):
        """遍历 (definition, handler) 对，用于构建子 agent 工具集"""
        for defn in self._definitions:
            yield defn, self._handlers[defn["name"]]

    def execute(self, name: str, args: dict) -> str:
        handler = self._handlers.get(name)
        if not handler:
            return f"Unknown tool: {name}"
        try:
            return handler(**args)
        except Exception as e:
            return f"Error: {e}"
