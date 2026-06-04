"""
Bash 工具 — 执行 shell 命令
"""

import subprocess
from pathlib import Path


BASH_SCHEMA = {
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]


def make_bash_handler(workdir: Path):
    def run_bash(command: str) -> str:
        if any(d in command for d in DENY_LIST):
            return "Error: Dangerous command blocked"
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
