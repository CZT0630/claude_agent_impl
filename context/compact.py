"""
上下文压缩管线 — 四层递进压缩

执行顺序 (匹配 Claude Code 源码):
    budget → snip → micro → auto

每层都是零 API 调用，只有 L4 和 Emergency 才调 LLM。
"""

import json
import random
import time
from pathlib import Path
from anthropic import Anthropic


class CompactPipeline:
    def __init__(
        self,
        client: Anthropic,
        model: str,
        workdir: Path,
        max_messages: int = 50,
        keep_recent: int = 10,
        persist_threshold: int = 2000,
        token_threshold: int = 100000,
    ):
        self.client = client
        self.model = model
        self.workdir = workdir

        # 压缩参数
        self.max_messages = max_messages      # L1: 超过此数触发 snip
        self.keep_recent = keep_recent        # L2: 保留最近 N 条 tool_result
        self.persist_threshold = persist_threshold  # L3: 超过此字符数持久化
        self.token_threshold = token_threshold      # L4: 估算 token 超过此数触发 compact

        # 持久化目录
        self.transcripts_dir = workdir / ".transcripts"
        self.tool_results_dir = workdir / ".task_outputs" / "tool-results"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.tool_results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, messages: list) -> list:
        """
        执行完整压缩管线: budget → snip → micro → auto
        返回压缩后的 messages (原地修改 + 返回)
        """
        # L3: 大输出持久化 (最先跑，减少后续层的数据量)
        self._tool_result_budget(messages)

        # L1: 裁剪中间消息
        self._snip_compact(messages)

        # L2: 旧 tool_result 替换为占位符
        self._micro_compact(messages)

        # L4: 估算 token 数，超过阈值则 LLM 摘要
        if self._estimate_tokens(messages) > self.token_threshold:
            self._compact_history(messages)

        return messages

    # ------------------------------------------------------------------
    # L3: tool_result_budget — 大输出持久化到磁盘 (零 API)
    # ------------------------------------------------------------------

    def _tool_result_budget(self, messages: list):
        """扫描所有 tool_result，超过阈值的持久化到磁盘，只留预览"""
        for msg in messages:
            if not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content", "")
                if not isinstance(content, str):
                    continue
                # 已经持久化过的跳过
                if content.startswith("<persisted-output>"):
                    continue
                if len(content) > self.persist_threshold:
                    tool_use_id = block.get("tool_use_id", "unknown")
                    persisted = self._persist_output(tool_use_id, content)
                    block["content"] = persisted

    def _persist_output(self, tool_use_id: str, output: str) -> str:
        """将大输出写入磁盘，返回预览文本"""
        path = self.tool_results_dir / f"{tool_use_id}.txt"
        path.write_text(output, encoding="utf-8")
        preview = output[:2000]
        return (
            f"<persisted-output>\n"
            f"Full output saved to: {path}\n"
            f"Preview:\n{preview}\n"
            f"</persisted-output>"
        )

    # ------------------------------------------------------------------
    # L1: snip_compact — 消息数过多时裁剪中间消息 (零 API)
    # ------------------------------------------------------------------

    def _snip_compact(self, messages: list, max_messages: int | None = None):
        """消息数超过 max_messages 时，保留头尾，中间替换为占位文本"""
        limit = max_messages or self.max_messages
        if len(messages) <= limit:
            return

        keep_head = 3
        keep_tail = limit - keep_head
        snipped_count = len(messages) - keep_head - keep_tail

        # 统计被裁剪部分的工具调用
        snipped = messages[keep_head:-keep_tail]
        tool_calls = self._count_tool_calls(snipped)

        prefix = "[emergency snipped" if max_messages else "[snipped"
        summary_block = {
            "role": "user",
            "content": f"{prefix} {snipped_count} messages, {tool_calls} tool calls]",
        }

        messages[:] = messages[:keep_head] + [summary_block] + messages[-keep_tail:]

    def _count_tool_calls(self, messages: list) -> int:
        """统计消息列表中的工具调用次数"""
        count = 0
        for msg in messages:
            if not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    count += 1
                elif getattr(block, "type", None) == "tool_use":
                    count += 1
        return count

    # ------------------------------------------------------------------
    # L2: micro_compact — 旧 tool_result 替换为占位符 (零 API)
    # ------------------------------------------------------------------

    def _micro_compact(self, messages: list):
        """旧的 tool_result 内容替换为占位符，只保留最近 keep_recent 条"""
        tool_results = self._collect_tool_results(messages)
        if len(tool_results) <= self.keep_recent:
            return

        # 将旧的 tool_result 内容替换为占位符
        for block in tool_results[:-self.keep_recent]:
            content = block.get("content", "")
            if isinstance(content, str) and len(content) > 120:
                block["content"] = "[Earlier tool result compacted. Re-run if needed.]"

    def _collect_tool_results(self, messages: list) -> list[dict]:
        """收集所有 tool_result block 的引用 (原地可修改)"""
        results = []
        for msg in messages:
            if not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results.append(block)
        return results

    # ------------------------------------------------------------------
    # L4: compact_history — LLM 全文摘要 (1 次 API)
    # ------------------------------------------------------------------

    def _compact_history(self, messages: list):
        """用 LLM 对全部历史做摘要，替换 messages"""
        # 先保存完整记录
        self._write_transcript(messages)

        # 构建摘要请求
        history_text = self._serialize_messages(messages)
        summary = self._summarize_with_llm(history_text)

        # 替换为压缩后的内容
        messages[:] = [{"role": "user", "content": f"[Context compacted]\n\n{summary}"}]

    def _write_transcript(self, messages: list):
        """压缩前保存完整对话记录到磁盘"""
        ts = time.strftime("%Y%m%d_%H%M%S")
        suffix = random.randint(1000, 9999)
        path = self.transcripts_dir / f"transcript_{ts}_{suffix}.json"
        # 序列化时处理 Anthropic 对象
        serializable = self._make_serializable(messages)
        path.write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _serialize_messages(self, messages: list) -> str:
        """将 messages 序列化为可读文本"""
        lines = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str):
                lines.append(f"[{role}]: {content[:3000]}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "text":
                            lines.append(f"[{role}/text]: {block.get('text', '')[:2000]}")
                        elif btype == "tool_use":
                            lines.append(f"[{role}/tool_use]: {block.get('name', '?')}({json.dumps(block.get('input', {}), ensure_ascii=False)[:500]})")
                        elif btype == "tool_result":
                            c = block.get("content", "")
                            lines.append(f"[{role}/tool_result]: {str(c)[:500]}")
                    elif hasattr(block, "type"):
                        if block.type == "text":
                            lines.append(f"[{role}/text]: {getattr(block, 'text', '')[:2000]}")
                        elif block.type == "tool_use":
                            lines.append(f"[{role}/tool_use]: {block.name}({json.dumps(block.input, ensure_ascii=False)[:500]})")
        return "\n".join(lines)

    def _summarize_with_llm(self, history_text: str) -> str:
        """调用 LLM 生成对话摘要"""
        try:
            response = self.client.messages.create(
                model=self.model,
                system=(
                    "You are a summarizer. Compress the following conversation history "
                    "into a concise summary that preserves: "
                    "1) What was accomplished "
                    "2) What files were modified "
                    "3) What remains to be done "
                    "4) Any important decisions or constraints. "
                    "Keep it under 2000 words."
                ),
                messages=[{"role": "user", "content": history_text}],
                max_tokens=4000,
            )
            texts = [getattr(b, "text", "") for b in response.content if getattr(b, "text", "")]
            return "\n".join(texts) if texts else "(summary generation failed)"
        except Exception as e:
            return f"(summary failed: {e})"

    # ------------------------------------------------------------------
    # Emergency: reactive_compact — prompt_too_long 时触发 (1 次 API)
    # ------------------------------------------------------------------

    def reactive_compact(self, messages: list) -> list:
        """
        紧急压缩: API 返回 prompt_too_long 错误时调用。
        先做零 API 压缩，再用 LLM 摘要。
        """
        # 先做零 API 层
        self._tool_result_budget(messages)
        self._snip_compact(messages, max_messages=20)  # 更激进
        self._micro_compact(messages)

        # 如果还太长，用 LLM 摘要
        if self._estimate_tokens(messages) > self.token_threshold:
            self._compact_history(messages)

        return messages

    # ------------------------------------------------------------------
    # 工具: 大输出截断 (在工具执行后调用)
    # ------------------------------------------------------------------

    def truncate_output(self, output: str, tool_use_id: str = "inline") -> str:
        """
        工具返回结果后调用: 超过阈值则持久化，返回预览。
        用于在 agent loop 中替代直接截断。
        """
        if len(output) <= self.persist_threshold:
            return output
        return self._persist_output(tool_use_id, output)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _estimate_tokens(self, messages: list) -> int:
        """粗略估算 messages 的 token 数 (1 中文字符 ≈ 2 tokens, 1 英文单词 ≈ 1.3 tokens)"""
        text = self._serialize_messages(messages)
        # 简单估算: 字符数 / 3
        return len(text) // 3

    def _make_serializable(self, obj):
        """递归将 Anthropic 对象转为可序列化的 dict"""
        if isinstance(obj, list):
            return [self._make_serializable(item) for item in obj]
        if isinstance(obj, dict):
            return {k: self._make_serializable(v) for k, v in obj.items()}
        if hasattr(obj, "__dict__"):
            return {k: self._make_serializable(v) for k, v in obj.__dict__.items()
                    if not k.startswith("_")}
        return obj
