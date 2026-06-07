"""
Prompt 组装器 — 动态 SYSTEM prompt

设计:
    段落注册表 → 条件组装 → 缓存

用法:
    assembler = PromptAssembler()
    assembler.register("identity", "You are a coding agent.")
    assembler.register("memory", lambda ctx: ctx.get("memory_section", ""), condition=lambda ctx: bool(ctx.get("memory_section")))

    prompt = assembler.assemble({"memory_section": "<memories>...</memories>"})
"""

import json
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Section:
    """一个 prompt 段落"""
    name: str
    content: str | Callable[[dict], str]       # 静态字符串 或 动态函数
    condition: Callable[[dict], bool] | None    # None = 始终加载
    priority: int = 0                           # 越小越靠前


class PromptAssembler:
    """
    动态 SYSTEM prompt 组装器。

    段落按 priority 排序，条件满足时才加载。
    相同 context → 缓存命中 → 避免重复组装。
    """

    def __init__(self):
        # 段落注册表， 存所有注册的段落，key 是段落名
        self._sections: dict[str, Section] = {} 
        # 组装结果缓存, 缓存已组装的 prompt，避免相同 context 重复组装
        self._cache: dict[str, str] = {}

    def register(
        self,
        name: str,
        content: str | Callable[[dict], str],
        *,
        condition: Callable[[dict], bool] | None = None,
        priority: int = 0,
    ):
        """
        注册一个 prompt 段落。注册一个新段落到注册表。
        每次注册都会清空缓存，因为段落集变了，之前的缓存结果就无效了。
        同名重复注册会覆盖旧段落。

        Args:
            name: 段落名（唯一，重复注册覆盖）
            content: 静态字符串 或 (context) -> str 动态函数
            condition: None = 始终加载；否则 (context) -> bool
            priority: 排序权重，越小越靠前
        """
        self._sections[name] = Section(
            name=name,
            content=content,
            condition=condition,
            priority=priority,
        )
        self._cache.clear()  # 注册变更 → 清缓存

    def unregister(self, name: str):
        """移除一个段落"""
        self._sections.pop(name, None)
        self._cache.clear()

    def assemble(self, context: dict | None = None) -> str:
        """
        根据 context 组装完整 SYSTEM prompt。

        相同 context key → 缓存命中。
        """
        ctx = context or {}

        # 缓存 key: 将 context 序列化为稳定字符串
        cache_key = self._make_cache_key(ctx)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # 按 priority 排序，过滤条件
        ordered = sorted(self._sections.values(), key=lambda s: s.priority)
        parts: list[str] = []

        for section in ordered:
            # 检查条件
            if section.condition and not section.condition(ctx):
                continue

            # 解析内容
            if callable(section.content):
                text = section.content(ctx)
            else:
                text = section.content

            if text and text.strip():
                parts.append(text.strip())

        result = "\n\n".join(parts)
        self._cache[cache_key] = result
        return result

    def invalidate(self):
        """手动清缓存"""
        self._cache.clear()

    @property
    def section_names(self) -> list[str]:
        """返回所有已注册的段落名"""
        return list(self._sections.keys())

    def _make_cache_key(self, ctx: dict) -> str:
        """将 context 序列化为稳定的缓存 key"""
        # 只取影响 prompt 组装的 key，忽略不可序列化的值
        serializable = {}
        for k, v in ctx.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                serializable[k] = v
            elif isinstance(v, (list, dict)):
                try:
                    serializable[k] = json.dumps(v, ensure_ascii=False, sort_keys=True)
                except (TypeError, ValueError):
                    serializable[k] = str(v)
            else:
                serializable[k] = str(v)
        return json.dumps(serializable, ensure_ascii=False, sort_keys=True)
