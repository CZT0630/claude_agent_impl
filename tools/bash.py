"""
Bash 工具 — 执行 shell 命令

s02: 基础的同步执行
s13: 新增 run_in_background 参数，支持后台异步执行

设计选择: 没有新增工具，而是给 bash 加参数。
          因为后台执行只是 bash 的一种模式，不是独立能力。
          LLM 通过 run_in_background=true 主动选择异步。
"""

import subprocess
from pathlib import Path

from tools.background import BackgroundManager


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

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]


def make_bash_handler(workdir: Path, bg_manager: BackgroundManager | None = None):
    """
    构建 bash 工具的 handler。

    Args:
        workdir: 工作目录（所有命令在此目录下执行）
        bg_manager: 后台任务管理器（s13 新增）。None 时后台功能不可用。
    """
    def run_bash(command: str, run_in_background: bool = False) -> str:
        # Gate 1: 硬拒绝危险命令（与 s03 的 deny_list 一致）
        if any(d in command for d in DENY_LIST):
            return "Error: Dangerous command blocked"

        # s13: 后台执行 — 提交到 BackgroundManager，立即返回 bg_id
        if run_in_background and bg_manager:
            bg_id = bg_manager.start(command)
            return f"Background task started: {bg_id}\nUse check_background to poll for results."

        # 前台同步执行（原有逻辑，阻塞等待直到命令完成）
        try:
            r = subprocess.run(
                command, shell=True, cwd=workdir,
                capture_output=True, text=True, timeout=120
            )
            out = (r.stdout + r.stderr).strip()
            return out[:50000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: Timeout (120s)"
        except (FileNotFoundError, OSError) as e:
            return f"Error: {e}"
    return run_bash
