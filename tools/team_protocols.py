"""
Team Protocols — s16: 请求-回复协议 + 状态机

问题: s15 的队友只能发消息，没有结构化的请求-响应机制。
      Lead 无法可靠地请求队友关机、提交计划、或等待审批。
方案: 在 MessageBus 上层加协议层，用 request_id 关联请求和响应。

协议状态机:
    ProtocolRequest:
        request_id: str       # 唯一标识，关联请求和响应
        type: str             # shutdown | plan_request | plan_approval
        sender: str           # 发送者
        target: str           # 目标队友
        status: str           # pending → approved/rejected/completed
        created_at: float
        responded_at: float | None

协议流程 (以关机为例):
    Lead: send protocol "shutdown_request" → teammate inbox
        → teammate 收到 → 执行清理 → 发 "shutdown_response" → lead inbox
        → ProtocolManager.match_response(request_id) → status = completed

协议流程 (以计划审批为例):
    Lead: send protocol "plan_request" → teammate inbox
        → teammate 收到 → 生成计划 → 发 "plan_response" → lead inbox
        → Lead 看到计划 → review_plan(approve/reject) → 发 "plan_approval" → teammate
        → teammate 收到 → 执行/修改计划

新工具 (Lead):
    request_shutdown  — 向队友发送关机请求
    request_plan      — 请求队友提交计划
    review_plan       — 审批队友计划 (approve/reject)

队友增强:
    - 完成初始任务后进入 idle loop，等待新消息
    - dispatch_message 按消息类型路由到处理器
    - idle 60 秒无消息 → 自动关机
"""

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from tools.teams import MessageBus


# ── 协议数据模型 ─────────────────────────────────────────────────────

@dataclass
class ProtocolRequest:
    """一个协议请求的状态追踪"""
    request_id: str                  # 唯一标识
    type: str                        # shutdown | plan_request | plan_approval
    sender: str                      # 发送者 (通常是 "lead")
    target: str                      # 目标队友名字
    status: str = "pending"          # pending → approved/rejected/completed
    payload: dict = field(default_factory=dict)  # 请求内容
    created_at: float = field(default_factory=time.time)
    responded_at: float | None = None


# ── 协议管理器 ───────────────────────────────────────────────────────

class ProtocolManager:
    """
    管理所有协议请求的生命周期。

    职责:
        - 创建协议请求 (生成 request_id)
        - 发送协议消息到队友邮箱
        - 匹配响应 (通过 request_id)
        - 等待响应完成 (带超时)
    """

    def __init__(self, bus: MessageBus):
        self.bus = bus
        self._requests: dict[str, ProtocolRequest] = {}  # request_id → request
        self._lock = threading.Lock()

    def _gen_request_id(self) -> str:
        return f"req_{uuid.uuid4().hex[:12]}"

    def create_and_send(
        self,
        protocol_type: str,
        sender: str,
        target: str,
        payload: dict,
    ) -> str:
        """
        创建协议请求并发送到目标邮箱。

        Args:
            protocol_type: 协议类型 (shutdown_request, plan_request, plan_approval)
            sender: 发送者名字
            target: 目标队友名字
            payload: 协议内容

        Returns:
            request_id
        """
        request_id = self._gen_request_id()
        req = ProtocolRequest(
            request_id=request_id,
            type=protocol_type,
            sender=sender,
            target=target,
            payload=payload,
        )
        with self._lock:
            self._requests[request_id] = req

        # 通过 MessageBus 发送协议消息
        self.bus.send(
            to=target,
            msg_from=sender,
            msg_type=f"protocol:{protocol_type}",
            payload={"request_id": request_id, **payload},
        )
        return request_id

    def match_response(self, request_id: str, status: str, payload: dict | None = None):
        """
        匹配响应，更新请求状态。

        Args:
            request_id: 请求 ID
            status: 新状态 (approved, rejected, completed)
            payload: 响应内容
        """
        with self._lock:
            req = self._requests.get(request_id)
            if req:
                req.status = status
                req.responded_at = time.time()
                if payload:
                    req.payload.update(payload)

    def wait_for_response(self, request_id: str, timeout: float = 30.0) -> ProtocolRequest | None:
        """
        等待协议响应完成。

        Args:
            request_id: 请求 ID
            timeout: 超时秒数

        Returns:
            完成的请求，或 None（超时）
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                req = self._requests.get(request_id)
                if req and req.status != "pending":
                    return req
            time.sleep(0.5)
        return None

    def get_request(self, request_id: str) -> ProtocolRequest | None:
        with self._lock:
            return self._requests.get(request_id)

    def get_pending(self, target: str | None = None) -> list[ProtocolRequest]:
        """获取所有待处理的请求"""
        with self._lock:
            return [
                req for req in self._requests.values()
                if req.status == "pending"
                and (target is None or req.target == target)
            ]

    def summary(self) -> str:
        """返回所有协议请求的状态摘要"""
        with self._lock:
            if not self._requests:
                return "No protocol requests."

            lines = []
            for req in self._requests.values():
                icon = {"pending": "⏳", "approved": "✅", "rejected": "❌", "completed": "✅"}.get(
                    req.status, "❓"
                )
                age = time.time() - req.created_at
                lines.append(
                    f"  {icon} {req.request_id} [{req.type}] "
                    f"{req.sender}→{req.target} status={req.status} ({age:.0f}s)"
                )
            return "\n".join(lines)


# ── 协议消息处理 ─────────────────────────────────────────────────────

def is_protocol_message(msg: dict) -> bool:
    """检查消息是否是协议消息"""
    return msg.get("type", "").startswith("protocol:")


def parse_protocol_type(msg: dict) -> str:
    """从协议消息中提取协议类型"""
    return msg.get("type", "").removeprefix("protocol:")


def handle_protocol_message(
    msg: dict,
    bus: MessageBus,
    agent_name: str,
    protocol_mgr: ProtocolManager | None = None,
) -> str | None:
    """
    处理协议消息，返回响应内容（如果有）。

    Args:
        msg: 消息字典
        bus: MessageBus 实例
        agent_name: 当前 agent 名字
        protocol_mgr: ProtocolManager 实例（lead 用，可选）

    Returns:
        处理结果描述，或 None（非协议消息）
    """
    if not is_protocol_message(msg):
        return None

    protocol_type = parse_protocol_type(msg)
    request_id = msg["payload"].get("request_id", "")
    sender = msg.get("from", "unknown")

    # ── shutdown_request: 收到关机请求 ──
    if protocol_type == "shutdown_request":
        # 发送确认响应
        bus.send(
            to=sender,
            msg_from=agent_name,
            msg_type="protocol:shutdown_response",
            payload={"request_id": request_id, "approved": True},
        )
        if protocol_mgr:
            protocol_mgr.match_response(request_id, "completed")
        return f"Shutdown acknowledged for request {request_id}"

    # ── shutdown_response: 收到关机确认 ──
    if protocol_type == "shutdown_response":
        approved = msg["payload"].get("approved", False)
        if protocol_mgr:
            protocol_mgr.match_response(request_id, "completed" if approved else "rejected")
        return f"Shutdown {'confirmed' if approved else 'rejected'} for request {request_id}"

    # ── plan_request: 收到计划请求 ──
    if protocol_type == "plan_request":
        # 由队友的 LLM 处理，这里只标记为已收到
        return f"Plan requested by {sender} (request_id={request_id})"

    # ── plan_response: 收到计划响应 ──
    if protocol_type == "plan_response":
        plan = msg["payload"].get("plan", "(no plan)")
        if protocol_mgr:
            protocol_mgr.match_response(request_id, "pending_review", {"plan": plan})
        return f"Plan received from {sender}:\n{plan}"

    # ── plan_approval: 收到计划审批 ──
    if protocol_type == "plan_approval":
        approved = msg["payload"].get("approved", False)
        feedback = msg["payload"].get("feedback", "")
        if protocol_mgr:
            protocol_mgr.match_response(request_id, "approved" if approved else "rejected")
        return f"Plan {'approved' if approved else 'rejected'}: {feedback}"

    return f"Unknown protocol type: {protocol_type}"


# ── 工具 Schema ──────────────────────────────────────────────────────

REQUEST_SHUTDOWN_SCHEMA = {
    "name": "request_shutdown",
    "description": (
        "Send a graceful shutdown request to a teammate. "
        "The teammate will acknowledge and stop."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "teammate": {
                "type": "string",
                "description": "Name of the teammate to shut down.",
            },
        },
        "required": ["teammate"],
    },
}

REQUEST_PLAN_SCHEMA = {
    "name": "request_plan",
    "description": (
        "Request a teammate to submit a plan before starting work. "
        "The teammate will respond with their proposed plan via inbox."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "teammate": {
                "type": "string",
                "description": "Name of the teammate to request a plan from.",
            },
            "context": {
                "type": "string",
                "description": "Additional context or constraints for the plan.",
            },
        },
        "required": ["teammate"],
    },
}

REVIEW_PLAN_SCHEMA = {
    "name": "review_plan",
    "description": (
        "Review and approve or reject a teammate's proposed plan. "
        "Use after receiving a plan_response from a teammate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "teammate": {
                "type": "string",
                "description": "Name of the teammate whose plan to review.",
            },
            "approved": {
                "type": "boolean",
                "description": "Whether to approve (true) or reject (false) the plan.",
            },
            "feedback": {
                "type": "string",
                "description": "Feedback or modifications for the teammate.",
            },
        },
        "required": ["teammate", "approved"],
    },
}

PROTOCOL_STATUS_SCHEMA = {
    "name": "protocol_status",
    "description": "Show status of all protocol requests (shutdown, plan, etc).",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ── 工具 Handler ─────────────────────────────────────────────────────

def make_request_shutdown_handler(protocol_mgr: ProtocolManager, team_manager):
    """构建 request_shutdown 工具的 handler"""

    def run_request_shutdown(teammate: str) -> str:
        # 检查队友是否存在
        with team_manager._lock:
            if teammate not in team_manager._teammates:
                return f"Error: teammate '{teammate}' not found"
            if team_manager._teammates[teammate]["status"] != "running":
                return f"Teammate '{teammate}' is already {team_manager._teammates[teammate]['status']}"

        request_id = protocol_mgr.create_and_send(
            protocol_type="shutdown_request",
            sender="lead",
            target=teammate,
            payload={},
        )
        return f"Shutdown request sent to '{teammate}' (request_id={request_id}). Use check_inbox to see the response."

    return run_request_shutdown


def make_request_plan_handler(protocol_mgr: ProtocolManager, team_manager):
    """构建 request_plan 工具的 handler"""

    def run_request_plan(teammate: str, context: str = "") -> str:
        with team_manager._lock:
            if teammate not in team_manager._teammates:
                return f"Error: teammate '{teammate}' not found"

        request_id = protocol_mgr.create_and_send(
            protocol_type="plan_request",
            sender="lead",
            target=teammate,
            payload={"context": context},
        )
        return f"Plan request sent to '{teammate}' (request_id={request_id}). Use check_inbox to see the response."

    return run_request_plan


def make_review_plan_handler(protocol_mgr: ProtocolManager, bus: MessageBus):
    """构建 review_plan 工具的 handler"""

    def run_review_plan(teammate: str, approved: bool, feedback: str = "") -> str:
        # 找到目标队友的最新 plan_request
        pending = [
            req for req in protocol_mgr.get_pending()
            if req.type == "plan_request" and req.target == teammate
        ]
        if not pending:
            # 也可以直接发审批消息，不关联 request
            request_id = protocol_mgr.create_and_send(
                protocol_type="plan_approval",
                sender="lead",
                target=teammate,
                payload={"approved": approved, "feedback": feedback},
            )
            return f"Plan {'approved' if approved else 'rejected'} for '{teammate}' (request_id={request_id})"

        # 关联到最新的 plan request
        req = max(pending, key=lambda r: r.created_at)
        protocol_mgr.match_response(req.request_id, "approved" if approved else "rejected")
        bus.send(
            to=teammate,
            msg_from="lead",
            msg_type="protocol:plan_approval",
            payload={
                "request_id": req.request_id,
                "approved": approved,
                "feedback": feedback,
            },
        )
        action = "approved" if approved else "rejected"
        return f"Plan {action} for '{teammate}': {feedback or '(no feedback)'}"

    return run_review_plan


def make_protocol_status_handler(protocol_mgr: ProtocolManager):
    """构建 protocol_status 工具的 handler"""

    def run_protocol_status() -> str:
        return protocol_mgr.summary()

    return run_protocol_status
