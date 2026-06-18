"""
Bash 工具 — 执行 shell 命令

s02: 基础的同步执行
s13: 新增 run_in_background 参数，支持后台异步执行
s21: 沙箱隔离执行，跨平台自动选择最优方案

设计选择: 没有新增工具，而是给 bash 加参数。
          因为后台执行只是 bash 的一种模式，不是独立能力。
          LLM 通过 run_in_background=true 主动选择异步。
"""

from pathlib import Path

from tools.background import BackgroundManager
from tools.sandbox import Sandbox


BASH_SCHEMA = {
    "name": "bash",
    "description": "Run a shell command. Set run_in_background=true for long-running commands.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute."},
            "run_in_background": {
                "type": "boolean",
                "description": "Run in background (for slow operations). Returns immediately with a task ID.",
                "default": False,  # 默认同步，原有行为完全不变
            },
        },
        "required": ["command"],
    },
}


def make_bash_handler(
    workdir: Path,
    bg_manager: BackgroundManager | None = None,
    sandbox_level: str = "auto",
):
    """
    构建 bash 工具的 handler。

    Args:
        workdir: 工作目录（所有命令在此目录下执行）
        bg_manager: 后台任务管理器（s13 新增）。None 时后台功能不可用。
        sandbox_level: 沙箱级别（s21 新增）。"auto" 自动检测 | "off" 关闭。
    """
    sandbox = Sandbox(workdir, level=sandbox_level)

    def run_bash(command: str, run_in_background: bool = False) -> str:
        # s13: 后台执行 — 提交到 BackgroundManager，立即返回 bg_id
        if run_in_background and bg_manager:
            bg_id = bg_manager.start(command, executor=sandbox.execute)
            return f"Background task started: {bg_id}\nUse check_background to poll for results."

        # s21: 沙箱执行（包含黑名单检查 + 平台隔离）
        return sandbox.execute(command)

    return run_bash
