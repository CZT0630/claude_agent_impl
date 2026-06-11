"""
MCP Plugin — s19: 外部能力合体

问题: 内置工具有限，但外部世界有无数能力（数据库、文档搜索、API 调用…），
      手动一个个写 handler 太慢，且每个服务的接口都不同。
方案: 用 MCP (Model Context Protocol) 标准协议，自动发现并调用外部工具。

架构:
    connect_mcp("docs") → MCPClient discovers tools →
    assemble_tool_pool → [builtin..., mcp__docs__search, mcp__docs__get_version]
    agent_loop uses assembled pool

MCP 服务器配置 (.mcp.json):
    {
        "servers": {
            "docs": {
                "command": "mcp-server-docs",
                "args": ["--port", "3000"],
                "env": {"API_KEY": "..."}
            },
            "db": {
                "command": "mcp-server-sqlite",
                "args": ["--db", "data.db"]
            }
        }
    }

工具命名: mcp__{server}__{tool}
    例: mcp__docs__search, mcp__db__query

关键概念:
    - MCP 工具自动发现，不需要手动注册
    - MCP 工具有 readOnly/destructive 标注，可接入权限管线
    - 工具名带 mcp__ 前缀，与内置工具无冲突
    - 连接生命周期由 MCPManager 管理
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp import ClientSession, ListToolsResult, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent


# ── MCP 服务器配置 ──────────────────────────────────────────────────

class MCPServerConfig:
    """单个 MCP 服务器的配置"""

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.args: list[str] = args or []
        self.env: dict[str, str] | None = env

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "MCPServerConfig":
        return cls(
            name=name,
            command=data["command"],
            args=data.get("args"),
            env=data.get("env"),
        )


def load_mcp_configs(workdir: Path) -> list[MCPServerConfig]:
    """
    从 .mcp.json 加载 MCP 服务器配置。

    搜索顺序:
        1. {workdir}/.mcp.json
        2. {workdir}/.claude/mcp.json

    Returns:
        MCPServerConfig 列表
    """
    candidates = [
        workdir / ".mcp.json",
        workdir / ".claude" / "mcp.json",
    ]

    for path in candidates:
        if path.exists():
            try:
                data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                servers: dict[str, Any] = data.get("servers", {})
                return [
                    MCPServerConfig.from_dict(name, cfg)
                    for name, cfg in servers.items()
                ]
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                print(f"\033[31m[mcp] Failed to parse {path}: {e}\033[0m")

    return []


# ── MCP 工具信息 ────────────────────────────────────────────────────

class MCPToolInfo:
    """从 MCP 服务器发现的工具元数据"""

    def __init__(self, server_name: str, name: str, description: str,
                 input_schema: dict[str, Any], annotations: dict[str, Any] | None = None):
        self.server_name = server_name
        self.name = name
        self.full_name = f"mcp__{server_name}__{name}"
        self.description = description
        self.input_schema = input_schema
        self.annotations: dict[str, Any] = annotations or {}

    @property
    def is_read_only(self) -> bool:
        return bool(self.annotations.get("readOnlyHint", False))

    @property
    def is_destructive(self) -> bool:
        return bool(self.annotations.get("destructiveHint", False))

    def to_tool_param(self) -> dict[str, Any]:
        """转换为 Anthropic ToolParam 格式"""
        return {
            "name": self.full_name,
            "description": f"[MCP:{self.server_name}] {self.description}",
            "input_schema": self.input_schema,
        }


# ── MCP 连接 ────────────────────────────────────────────────────────

class MCPConnection:
    """
    单个 MCP 服务器的连接。

    管理与一个 MCP 服务器的 stdio 通信：
        - 连接（启动子进程）
        - 工具发现
        - 工具调用
        - 断开（终止子进程）
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.tools: list[MCPToolInfo] = []
        self._session: ClientSession | None = None
        self._cleanup: Any = None  # async context manager cleanup callback
        self._connected = False

    async def connect(self) -> list[MCPToolInfo]:
        """
        连接到 MCP 服务器并发现工具。

        Returns:
            发现的工具列表
        """
        server_params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=self.config.env,
        )

        # stdio_client 是 async context manager，手动管理生命周期
        # 进入 stdio_client → 拿到 read/write stream → 进入 ClientSession
        cm = stdio_client(server_params)
        streams = await cm.__aenter__()
        read_stream, write_stream = streams

        self._session = ClientSession(read_stream, write_stream)
        await self._session.__aenter__()

        # 初始化连接
        await self._session.initialize()

        # 发现工具
        result: ListToolsResult = await self._session.list_tools()
        self.tools = []
        for tool in result.tools:
            # annotations 可能为 None
            ann_dict: dict[str, Any] | None = None
            if tool.annotations is not None:
                ann_dict = tool.annotations.model_dump(exclude_none=True)

            tool_info = MCPToolInfo(
                server_name=self.config.name,
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema if isinstance(tool.inputSchema, dict) else {},
                annotations=ann_dict,
            )
            self.tools.append(tool_info)

        self._connected = True
        # 保存 cleanup 回调，disconnect 时调用
        self._cleanup = cm.__aexit__
        return self.tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """
        调用 MCP 服务器上的工具。

        Args:
            tool_name: 工具名（不含 mcp__ 前缀）
            arguments: 工具参数

        Returns:
            工具执行结果文本
        """
        if not self._session or not self._connected:
            return f"Error: MCP server '{self.config.name}' is not connected"

        try:
            result = await self._session.call_tool(tool_name, arguments)
            parts: list[str] = []
            for item in result.content:
                if isinstance(item, TextContent):
                    parts.append(item.text)
                else:
                    parts.append(str(item))
            return "\n".join(parts) if parts else "(empty result)"
        except Exception as e:
            return f"Error calling MCP tool '{tool_name}': {e}"

    async def disconnect(self) -> None:
        """断开与 MCP 服务器的连接"""
        self._connected = False
        if self._session:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._cleanup:
            try:
                await self._cleanup(None, None, None)
            except Exception:
                pass
            self._cleanup = None
        self.tools = []

    @property
    def is_connected(self) -> bool:
        return self._connected


# ── MCP 管理器 ──────────────────────────────────────────────────────

class MCPManager:
    """
    管理所有 MCP 服务器连接。

    生命周期:
        connect(server_name) → 连接并发现工具
        disconnect(server_name) → 断开连接
        call_tool(full_name, args) → 路由到正确的服务器

    工具注册:
        发现的工具自动注册到 ToolRegistry，
        命名格式: mcp__{server}__{tool}
    """

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self._connections: dict[str, MCPConnection] = {}
        self._tools: dict[str, MCPToolInfo] = {}  # full_name → MCPToolInfo

    def connect_sync(self, server_name: str) -> str:
        """
        同步连接到 MCP 服务器（供工具 handler 调用）。

        Args:
            server_name: 服务器名（对应 .mcp.json 中的 key）

        Returns:
            连接结果描述
        """
        configs = load_mcp_configs(self.workdir)
        config_map: dict[str, MCPServerConfig] = {c.name: c for c in configs}

        if server_name not in config_map:
            available = ", ".join(config_map.keys()) if config_map else "(none)"
            return f"Error: MCP server '{server_name}' not found in .mcp.json. Available: {available}"

        if server_name in self._connections and self._connections[server_name].is_connected:
            return f"MCP server '{server_name}' is already connected."

        config = config_map[server_name]
        conn = MCPConnection(config)

        try:
            tools = asyncio.run(conn.connect())
            self._connections[server_name] = conn

            for tool in tools:
                self._tools[tool.full_name] = tool

            tool_names = [t.full_name for t in tools]
            return (
                f"Connected to MCP server '{server_name}'. "
                f"Discovered {len(tools)} tools: {', '.join(tool_names)}"
            )
        except Exception as e:
            return f"Error connecting to MCP server '{server_name}': {e}"

    def disconnect_sync(self, server_name: str) -> str:
        """
        同步断开 MCP 服务器连接。

        Args:
            server_name: 服务器名

        Returns:
            断开结果描述
        """
        if server_name not in self._connections:
            return f"MCP server '{server_name}' is not connected."

        conn = self._connections[server_name]
        try:
            asyncio.run(conn.disconnect())
        except Exception:
            pass

        to_remove = [k for k, v in self._tools.items() if v.server_name == server_name]
        for k in to_remove:
            del self._tools[k]

        del self._connections[server_name]
        return f"Disconnected from MCP server '{server_name}'."

    def call_tool_sync(self, full_name: str, arguments: dict[str, Any]) -> str:
        """
        同步调用 MCP 工具。

        Args:
            full_name: 全局工具名 (mcp__{server}__{tool})
            arguments: 工具参数

        Returns:
            工具执行结果
        """
        tool_info = self._tools.get(full_name)
        if not tool_info:
            return f"Error: Unknown MCP tool '{full_name}'"

        server_name = tool_info.server_name
        conn = self._connections.get(server_name)
        if not conn or not conn.is_connected:
            return f"Error: MCP server '{server_name}' is not connected"

        try:
            return asyncio.run(conn.call_tool(tool_info.name, arguments))
        except Exception as e:
            return f"Error calling MCP tool '{full_name}': {e}"

    def list_tools_sync(self) -> str:
        """列出所有已发现的 MCP 工具"""
        if not self._tools:
            connected = [s for s, c in self._connections.items() if c.is_connected]
            if connected:
                return f"Connected servers: {', '.join(connected)}. No tools discovered."
            return "No MCP servers connected. Use mcp_connect to connect."

        lines: list[str] = []
        by_server: dict[str, list[MCPToolInfo]] = {}
        for tool in self._tools.values():
            by_server.setdefault(tool.server_name, []).append(tool)

        for server, tools in by_server.items():
            lines.append(f"\n\033[1m{server}\033[0m ({len(tools)} tools):")
            for t in tools:
                flags: list[str] = []
                if t.is_read_only:
                    flags.append("read-only")
                if t.is_destructive:
                    flags.append("destructive")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                desc = t.description[:80] + ("..." if len(t.description) > 80 else "")
                lines.append(f"  {t.full_name}{flag_str}")
                lines.append(f"    {desc}")

        return "\n".join(lines)

    def get_all_tool_params(self) -> list[dict[str, Any]]:
        """获取所有 MCP 工具的 ToolParam（用于注册到 ToolRegistry）"""
        return [t.to_tool_param() for t in self._tools.values()]

    def get_tool_names(self) -> list[str]:
        """获取所有 MCP 工具的全名列表"""
        return list(self._tools.keys())

    @property
    def has_connections(self) -> bool:
        return any(c.is_connected for c in self._connections.values())

    def auto_connect_all(self) -> str:
        """
        自动连接 .mcp.json 中配置的所有服务器。

        Returns:
            连接结果摘要
        """
        configs = load_mcp_configs(self.workdir)
        if not configs:
            return "No MCP servers configured in .mcp.json"

        results: list[str] = []
        for config in configs:
            result = self.connect_sync(config.name)
            results.append(result)

        return "\n".join(results)

    def shutdown_all(self) -> None:
        """关闭所有连接（用于清理退出）"""
        for name in list(self._connections.keys()):
            try:
                self.disconnect_sync(name)
            except Exception:
                pass


# ── 工具 Schema ─────────────────────────────────────────────────────

MCP_CONNECT_SCHEMA = {
    "name": "mcp_connect",
    "description": (
        "Connect to an MCP (Model Context Protocol) server and discover its tools. "
        "Server configs are read from .mcp.json. "
        "After connecting, the server's tools become available as mcp__{server}__{tool}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "server_name": {
                "type": "string",
                "description": "Name of the MCP server to connect (key in .mcp.json).",
            },
        },
        "required": ["server_name"],
    },
}

MCP_DISCONNECT_SCHEMA = {
    "name": "mcp_disconnect",
    "description": (
        "Disconnect from an MCP server and remove its tools from the pool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "server_name": {
                "type": "string",
                "description": "Name of the MCP server to disconnect.",
            },
        },
        "required": ["server_name"],
    },
}

MCP_LIST_TOOLS_SCHEMA = {
    "name": "mcp_list_tools",
    "description": (
        "List all available MCP tools from connected servers. "
        "Shows tool names, descriptions, and flags (read-only, destructive)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ── 工具 Handler ────────────────────────────────────────────────────

def make_mcp_handlers(mcp_manager: MCPManager, registry: Any = None) -> dict[str, Any]:
    """
    返回 MCP 管理工具的 handler。

    Args:
        mcp_manager: MCPManager 实例
        registry: ToolRegistry（可选，用于动态注册 MCP 工具）
    """

    def run_mcp_connect(server_name: str) -> str:
        result = mcp_manager.connect_sync(server_name)

        # 动态注册发现的工具到 ToolRegistry
        if registry and "Connected" in result:
            _register_mcp_tools(mcp_manager, registry)

        return result

    def run_mcp_disconnect(server_name: str) -> str:
        result = mcp_manager.disconnect_sync(server_name)

        # 从 registry 移除该服务器的工具
        if registry and "Disconnected" in result:
            _unregister_mcp_tools(registry, server_name)

        return result

    def run_mcp_list_tools() -> str:
        return mcp_manager.list_tools_sync()

    return {
        "mcp_connect": run_mcp_connect,
        "mcp_disconnect": run_mcp_disconnect,
        "mcp_list_tools": run_mcp_list_tools,
    }


def _register_mcp_tools(mcp_manager: MCPManager, registry: Any) -> None:
    """将 MCP 工具动态注册到 ToolRegistry"""
    existing = {d["name"] for d in registry.get_definitions()}
    for tool_info in mcp_manager._tools.values():
        full_name = tool_info.full_name
        if full_name in existing:
            continue

        def _make_handler(fname: str = full_name) -> Any:
            def handler(**kwargs: Any) -> str:
                return mcp_manager.call_tool_sync(fname, kwargs)
            return handler

        registry.register(
            name=full_name,
            description=f"[MCP:{tool_info.server_name}] {tool_info.description}",
            input_schema=tool_info.input_schema,
            handler=_make_handler(),
        )


def _unregister_mcp_tools(registry: Any, server_name: str) -> None:
    """从 ToolRegistry 移除指定服务器的 MCP 工具"""
    prefix = f"mcp__{server_name}__"
    # ToolRegistry 没有 unregister 方法，直接操作内部数据
    to_remove = [
        d["name"] for d in registry.get_definitions()
        if d["name"].startswith(prefix)
    ]
    for name in to_remove:
        if name in registry._handlers:
            del registry._handlers[name]
    registry._definitions = [
        d for d in registry._definitions
        if not d["name"].startswith(prefix)
    ]
