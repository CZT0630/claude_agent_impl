#!/usr/bin/env python3
"""
Claude Agent — s02: 多工具 + 分发映射

在 s01 基础上新增 4 个工具 + TOOL_HANDLERS 分发映射。
agent_loop 不变，只改工具执行部分。

运行:
    pip install anthropic python-dotenv
    复制 .env.example 为 .env, 填入 ANTHROPIC_API_KEY
    python main.py
"""

import os
import subprocess
import glob as glob_module
from pathlib import Path

from anthropic import Anthropic
from anthropic.types import ToolParam
from dotenv import load_dotenv

# ── 配置 ──────────────────────────────────────────────────

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
WORKDIR = Path.cwd()

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


# ── 路径安全校验 ──────────────────────────────────────────

def safe_path(p: str) -> Path:
    """防止路径穿越攻击，确保路径在工作目录内"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR.resolve()):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# ── 工具定义: 5 个工具 ────────────────────────────────────

TOOLS: list[ToolParam] = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "Find files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
]


# ── 工具执行 ──────────────────────────────────────────────

def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    try:
        results = []
        for match in glob_module.glob(pattern, root_dir=WORKDIR, recursive=True):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR.resolve()):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# ── 分发映射: 工具名 → 执行函数 ───────────────────────────

TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


# ── 核心循环（与 s01 结构一致）────────────────────────────

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type == "tool_use" and hasattr(block, "input"):
                # s02 改动: 用 TOOL_HANDLERS 查表分发，替代硬编码的 run_bash
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"

                print(f"\033[33m> {block.name}\033[0m")
                try:
                    print(str(output)[:200])
                except UnicodeEncodeError:
                    print(str(output)[:200].encode("utf-8", errors="replace").decode("utf-8"))

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
        messages.append({"role": "user", "content": results})


# ── CLI 入口 ──────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Claude Agent — model: {MODEL}")
    print(f"Working directory: {WORKDIR}")
    print("输入问题, 回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36m>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)

        last = history[-1].get("content", [])
        if isinstance(last, list):
            for block in last:
                if getattr(block, "type", None) == "text" and hasattr(block, "text"):
                    try:
                        print(block.text)
                    except UnicodeEncodeError:
                        print(block.text.encode("utf-8", errors="replace").decode("utf-8"))
        print()
