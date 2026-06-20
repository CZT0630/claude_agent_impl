"""
Agent 核心循环 — 整个 agent 的心脏

模式:
    while stop_reason == "tool_use":
        collect_background_results()  ← s13: 收集后台任务通知
        collect_cron_triggers()       ← s14: 收集定时任务触发
        collect_team_messages()       ← s15: 收集队友消息
        compact(messages)             ← s08: 四层压缩管线
        system = assemble(ctx)        ← s10: 动态 prompt 组装
        response = LLM(messages, tools)   ← s11: 带错误恢复
        trigger hooks
        execute tools
        append results
"""

import json
import re
from anthropic import Anthropic
from agent.events import (
    MODEL_FINISHED,
    MODEL_STARTED,
    TOOL_FINISHED,
    TOOL_STARTED,
)
from tools.registry import ToolRegistry
from tools.result import ToolResult
from tools.background import BackgroundManager  # s13: 后台任务管理器
from tools.cron import CronScheduler            # s14: 定时调度器
from tools.teams import TeamManager             # s15: 队友管理器
from tools.mcp import MCPManager               # s19: MCP 外部工具管理器
from hooks.manager import HookManager
from context.compact import CompactPipeline
from memory.manager import MemoryManager
from prompt.assembler import PromptAssembler
from agent.interceptor import InterceptorChain
from agent.state import LoopState
from agent.recovery import (
    RecoveryState,
    handle_max_tokens,
    handle_prompt_too_long,
    handle_rate_limit,
    should_switch_fallback,
    classify_error,
)


class AgentLoop:
    def __init__(
        self,
        client: Anthropic,
        model: str,
        system_prompt: str | PromptAssembler,
        tool_registry: ToolRegistry,
        hook_manager: HookManager | None = None,
        compact_pipeline: CompactPipeline | None = None,
        memory_manager: MemoryManager | None = None,
        bg_manager: BackgroundManager | None = None,
        cron_scheduler: CronScheduler | None = None,
        team_manager: TeamManager | None = None,
        mcp_manager: MCPManager | None = None,
        max_tokens: int = 8000,
        max_rounds: int | None = None,
        fallback_model: str | None = None,
        interceptors: InterceptorChain | None = None,
    ):
        self.client = client
        self.model = model
        self.tools = tool_registry
        self.hooks = hook_manager or HookManager()
        self.compact = compact_pipeline
        self.memory = memory_manager
        self.bg = bg_manager
        self.cron = cron_scheduler
        self.team = team_manager
        self.mcp_manager = mcp_manager
        self.max_tokens = max_tokens
        self.max_rounds = max_rounds
        self.rounds_since_todo = 0
        self.interceptors = interceptors or InterceptorChain()

        # s11: 错误恢复状态
        self.recovery = RecoveryState()
        self.fallback_model = fallback_model

        # s10: 支持动态 PromptAssembler 或静态字符串
        if isinstance(system_prompt, PromptAssembler):
            self._assembler = system_prompt
            self._static_prompt = None
        else:
            self._assembler = None
            self._static_prompt = system_prompt

    def _get_system_prompt(self, context: dict | None = None) -> str:
        """获取当前 system prompt（动态组装或静态返回）"""
        if self._assembler:
            return self._assembler.assemble(context or {})
        return self._static_prompt or ""

    def _build_prompt_context(self, memory_context: str = "") -> dict:
        """构建给 PromptAssembler 的上下文字典"""
        return {
            "has_memories": bool(self.memory and self.memory.has_memories),
            "memory_context": memory_context,
        }

    def _get_current_model(self) -> str:
        """返回当前应使用的模型（可能因 fallback 切换）"""
        if self.fallback_model and should_switch_fallback(self.recovery):
            return self.fallback_model
        return self.model

    @staticmethod
    def _artifact_refs_from_output(output: str) -> list[str]:
        match = re.search(r"Full output saved to:\s*(.+)", output)
        return [match.group(1).strip()] if match else []

    # ── 核心 LLM 调用（带恢复） ────────────────────────────────────

    def _call_llm(
        self,
        system_prompt: str,
        messages: list,
        state: LoopState | None = None,
    ):
        """
        调用 LLM，内置三条恢复路径。

        Path 1: max_tokens   → 升级 token + continuation prompt
        Path 2: prompt_too_long → reactive compact
        Path 3: 429/529      → 指数退避重试

        Returns:
            response 对象

        Raises:
            Exception: 所有恢复路径用尽后仍失败
        """
        current_max = self.max_tokens

        while True:
            model = self._get_current_model()
            request = {
                "model": model,
                "system": system_prompt,
                "messages": messages,
                "tools": self.tools.get_definitions(),
                "max_tokens": current_max,
                "state": state,
            }

            try:
                if state:
                    state.emit_event(
                        MODEL_STARTED,
                        payload={
                            "model": model,
                            "max_tokens": current_max,
                            "message_count": len(messages),
                            "tool_count": len(request["tools"]),
                        },
                    )
                response = self.interceptors.intercept_model(
                    request,
                    lambda req: self.client.messages.create(
                        model=req["model"],
                        system=req["system"],
                        messages=req["messages"],
                        tools=req["tools"],
                        max_tokens=req["max_tokens"],
                    ),
                )
                if state:
                    state.record_model_call(request, response=response)
                    state.emit_event(
                        MODEL_FINISHED,
                        payload={
                            "model": model,
                            "stop_reason": getattr(response, "stop_reason", None),
                            "usage": getattr(response, "usage", None),
                        },
                    )

                # 调用成功，重置 529 计数
                self.recovery.on_success()

                # ── Path 1: max_tokens 截断 ──
                if response.stop_reason == "max_tokens":
                    new_max, should_retry = handle_max_tokens(
                        self.recovery, messages, current_max,
                    )
                    if should_retry:
                        current_max = new_max
                        print(f"\033[33m[recovery] max_tokens hit, "
                              f"escalated to {current_max}, "
                              f"continuation={self.recovery.continuation_count}\033[0m")
                        continue
                    # 恢复用尽，返回当前 response
                    return response

                # 正常返回
                return response

            except Exception as e:
                if state:
                    state.record_model_call(request, error=e)
                    state.emit_event(
                        MODEL_FINISHED,
                        severity="error",
                        message=str(e),
                        payload={"model": model},
                        error_code=type(e).__name__,
                    )
                error_type = classify_error(e)

                # ── Path 2: prompt_too_long ──
                if error_type == "prompt_too_long":
                    should_retry = handle_prompt_too_long(
                        self.recovery, self.compact, messages,
                    )
                    if should_retry:
                        print(f"\033[33m[recovery] prompt_too_long, "
                              f"reactive compact done, retrying...\033[0m")
                        continue
                    raise

                # ── Path 3: 速率限制 ──
                if error_type in ("rate_limit_429", "rate_limit_529"):
                    is_529 = (error_type == "rate_limit_529")
                    should_retry = handle_rate_limit(self.recovery, is_529)

                    if should_switch_fallback(self.recovery) and self.fallback_model:
                        print(f"\033[31m[recovery] too many 529s, "
                              f"switching to fallback: {self.fallback_model}\033[0m")
                        continue  # 下一轮会用 fallback_model

                    if should_retry:
                        print(f"\033[33m[recovery] {error_type}, "
                              f"retry {self.recovery.retry_count}/{10}, "
                              f"waiting...\033[0m")
                        continue
                    raise

                # 未知错误，不恢复
                raise

    # ── 主循环 ─────────────────────────────────────────────────────

    def run(self, messages: list, state: LoopState | None = None) -> list:
        for _ in range(self.max_rounds or 10**9):
            self.rounds_since_todo += 1

            # ── s13: 收集已完成的后台任务，注入通知 ───────────────────
            # 为什么放在循环开头？
            #   第 N 轮: LLM 发起后台命令 → 返回 bg_id → 继续思考
            #   第 N+1 轮: collect() → 没完成 → 继续思考别的
            #   第 N+2 轮: collect() → 完成了！→ 注入 <task_notification>
            #   第 N+3 轮: LLM 看到通知，告知用户结果
            # Agent 不需要主动轮询，通知自动出现在 messages 里。
            if self.bg:
                notifications = self.bg.collect()
                for notif in notifications:
                    messages.append({"role": "user", "content": notif})
                    print(f"\033[36m[bg] {notif[:120]}...\033[0m")

            # ── s14: 收集定时任务触发，注入消息 ─────────────────────
            # 为什么放在循环开头（和 bg collect 并列）？
            #   守护线程每秒检查 cron 表达式，匹配时 put 进队列
            #   agent loop 每轮开头 get_nowait() 消费队列
            #   这样调度器完全独立于 agent，不阻塞也不丢失
            if self.cron:
                cron_messages = self.cron.collect()
                for msg in cron_messages:
                    messages.append({"role": "user", "content": f"[cron trigger] {msg}"})
                    print(f"\033[36m[cron] {msg[:120]}...\033[0m")

            # ── s15: 收集队友消息，注入 inbox 通知 ───────────────────
            # 队友通过 send_message 发消息到 lead 的邮箱
            # agent loop 每轮开头自动读取，注入 messages
            # 这样 lead 不需要手动调 check_inbox 也能看到队友的消息
            if self.team:
                inbox = self.team.bus.receive("lead")
                for msg in inbox:
                    content = msg["payload"].get("content", json.dumps(msg["payload"]))
                    notification = (
                        f"[team message] from={msg['from']} type={msg['type']}\n{content}"
                    )
                    messages.append({"role": "user", "content": notification})
                    print(f"\033[36m[team] {msg['from']}: {content[:120]}...\033[0m")

            # ── s08: 四层压缩管线 (budget → snip → micro → auto) ─────
            if self.compact:
                self.compact.run(messages)

            # s09: 选择相关记忆注入上下文（在压缩之后、LLM 调用之前）
            memory_context = ""
            if self.memory:
                memory_context = self.memory.select(messages)
                if memory_context:
                    messages.append({"role": "user", "content": memory_context})

            # s10: 动态组装 system prompt
            prompt_ctx = self._build_prompt_context(memory_context)
            system_prompt = self._get_system_prompt(prompt_ctx)

            # UserPromptSubmit hook — nag reminder 等
            injected = self.hooks.trigger("UserPromptSubmit", messages)
            if injected:
                messages.append({"role": "user", "content": injected})

            # s11: 调用 LLM（带三条恢复路径）
            response = self._call_llm(system_prompt, messages, state=state)

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

                    if state:
                        state.emit_event(
                            TOOL_STARTED,
                            payload={
                                "name": block.name,
                                "tool_use_id": block.id,
                                "input": block.input,
                            },
                        )

                    # PreToolUse hook（权限检查在这里）
                    blocked = self.hooks.trigger("PreToolUse", block)
                    if blocked:
                        blocked_result = ToolResult.failure(
                            "TOOL_BLOCKED",
                            stderr=str(blocked),
                            metadata={"tool": block.name},
                        )
                        if state:
                            state.record_tool_event(
                                block.name,
                                blocked_result.to_dict(),
                                tool_use_id=block.id,
                                ok=False,
                                input=block.input,
                            )
                            state.emit_event(
                                TOOL_FINISHED,
                                severity="warning",
                                message=str(blocked),
                                payload={
                                    "name": block.name,
                                    "tool_use_id": block.id,
                                    "ok": False,
                                    "result": blocked_result.to_dict(),
                                },
                                error_code=blocked_result.error_code,
                            )
                        print(f"\033[31m⛔ {blocked}\033[0m")
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": blocked_result.to_text(),
                        })
                        continue

                    # 执行工具
                    tool_request = {
                        "name": block.name,
                        "input": block.input,
                        "block": block,
                        "state": state,
                    }

                    def execute_tool(req):
                        result = self.tools.execute_result(req["name"], req["input"])
                        req["_tool_result"] = result
                        return result.to_text()

                    output = self.interceptors.intercept_tool(
                        tool_request,
                        execute_tool,
                    )
                    tool_result = tool_request.get("_tool_result")
                    if not isinstance(tool_result, ToolResult):
                        tool_result = ToolResult.from_output(
                            output,
                            metadata={"tool": block.name},
                        )
                    output_text = output.to_text() if isinstance(output, ToolResult) else str(output)

                    # s08: 大输出持久化
                    if self.compact:
                        output_text = self.compact.truncate_output(output_text, block.id)

                    artifact_refs = list(tool_result.artifact_refs)
                    artifact_refs.extend(self._artifact_refs_from_output(output_text))

                    if state:
                        state.record_tool_event(
                            block.name,
                            tool_result.to_dict(),
                            tool_use_id=block.id,
                            ok=tool_result.ok,
                            input=block.input,
                        )
                        state.emit_event(
                            TOOL_FINISHED,
                            severity="info" if tool_result.ok else "error",
                            message=tool_result.error_code or "",
                            payload={
                                "name": block.name,
                                "tool_use_id": block.id,
                                "ok": tool_result.ok,
                                "result": tool_result.to_dict(),
                                "display_content": output_text,
                            },
                            error_code=tool_result.error_code,
                            artifact_refs=artifact_refs,
                        )

                    # PostToolUse hook
                    self.hooks.trigger("PostToolUse", block, output_text)

                    print(f"\033[33m> {block.name}\033[0m")
                    try:
                        print(output_text[:200])
                    except UnicodeEncodeError:
                        print(output_text[:200].encode("utf-8", errors="replace").decode("utf-8"))

                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output_text,
                    })
            messages.append({"role": "user", "content": results})
        return messages
