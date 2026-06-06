"""
上下文压缩模块 — 四层管线，便宜的先跑贵的后跑

L3: tool_result_budget  — 大输出持久化到磁盘 (零 API)
L1: snip_compact        — 消息数过多时裁剪中间消息 (零 API)
L2: micro_compact       — 旧 tool_result 替换为占位符 (零 API)
L4: compact_history     — LLM 全文摘要 (1 次 API)
Emergency: reactive_compact — prompt_too_long 时触发 (1 次 API)
"""

from context.compact import CompactPipeline

__all__ = ["CompactPipeline"]
