"""
Worktree Isolation — s18: 队友在独立 git worktree 中工作

每个任务可以绑定一个 git worktree，队友在其中工作，互不干扰。
worktree 结构: .worktrees/{name}/  分支: wt/{name}

工具:
    create_worktree  — 创建隔离工作区
    remove_worktree  — 删除工作区
    keep_worktree    — 保留工作区（不自动清理）
"""

import json
import re
import subprocess
import time
from pathlib import Path


# ── Worktree 管理器 ──────────────────────────────────────────────────

class WorktreeManager:
    """
    管理 git worktree 的创建、删除和事件日志。

    结构:
        {repo_root}/.worktrees/{name}/   ← worktree 目录
        {repo_root}/.worktrees/events.jsonl  ← 事件日志
    """

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.worktrees_dir = repo_root / ".worktrees"
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.worktrees_dir / "events.jsonl"
        self._kept: set[str] = set()  # 标记为保留的 worktree

    # ── 核心操作 ──────────────────────────────────────────────────

    def create(self, name: str, task_id: str | None = None) -> str:
        """
        创建一个 git worktree。

        Args:
            name: worktree 名称（同时用作目录名和分支名后缀）
            task_id: 可选，绑定的任务 ID

        Returns:
            worktree 的绝对路径
        """
        self._validate_name(name)
        wt_path = self.worktrees_dir / name

        if wt_path.exists():
            return f"Error: worktree '{name}' already exists"

        branch = f"wt/{name}"
        try:
            self._run_git(["worktree", "add", str(wt_path), "-b", branch])
        except subprocess.CalledProcessError as e:
            return f"Error creating worktree: {e.stderr.strip()}"

        self._log_event("create", name, task_id=task_id)
        return str(wt_path)

    def remove(self, name: str) -> str:
        """
        删除一个 git worktree（移除目录 + 清理 git 引用）。

        Args:
            name: worktree 名称
        """
        self._validate_name(name)
        wt_path = self.worktrees_dir / name

        if not wt_path.exists():
            return f"Error: worktree '{name}' not found"

        try:
            self._run_git(["worktree", "remove", str(wt_path), "--force"])
        except subprocess.CalledProcessError as e:
            return f"Error removing worktree: {e.stderr.strip()}"

        self._kept.discard(name)
        self._log_event("remove", name)
        return f"Removed worktree '{name}'"

    def keep(self, name: str) -> str:
        """标记 worktree 为保留，自动清理时跳过。"""
        self._validate_name(name)
        wt_path = self.worktrees_dir / name

        if not wt_path.exists():
            return f"Error: worktree '{name}' not found"

        self._kept.add(name)
        self._log_event("keep", name)
        return f"Worktree '{name}' marked as kept"

    def list_all(self) -> list[dict]:
        """列出所有 worktree 及其状态。"""
        result = []
        for entry in sorted(self.worktrees_dir.iterdir()):
            if entry.is_dir() and entry.name != "__pycache__":
                result.append({
                    "name": entry.name,
                    "path": str(entry),
                    "kept": entry.name in self._kept,
                    "branch": f"wt/{entry.name}",
                })
        return result

    def get_path(self, name: str) -> str | None:
        """获取 worktree 的绝对路径，不存在返回 None。"""
        wt_path = self.worktrees_dir / name
        return str(wt_path) if wt_path.exists() else None

    # ── 事件日志 ──────────────────────────────────────────────────

    def _log_event(self, action: str, name: str, **extra):
        entry = {
            "action": action,
            "worktree": name,
            "timestamp": time.time(),
        }
        entry.update(extra)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── 安全校验 ──────────────────────────────────────────────────

    @staticmethod
    def _validate_name(name: str):
        """防路径穿越 + 非法字符。"""
        if not name or not re.match(r"^[a-zA-Z0-9_-]+$", name):
            raise ValueError(
                f"Invalid worktree name '{name}': "
                "only alphanumeric, underscore, hyphen allowed"
            )

    # ── Git 执行 ──────────────────────────────────────────────────

    def _run_git(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + args,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=True,
        )


# ── 工具 Schema ─────────────────────────────────────────────────────

CREATE_WORKTREE_SCHEMA = {
    "name": "create_worktree",
    "description": (
        "Create an isolated git worktree for a task. "
        "The worktree gets its own branch (wt/<name>) and directory (.worktrees/<name>). "
        "Teammates can work in it without interfering with each other."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Worktree name (alphanumeric, underscore, hyphen).",
            },
            "task_id": {
                "type": "string",
                "description": "Optional task ID to bind to this worktree.",
            },
        },
        "required": ["name"],
    },
}

REMOVE_WORKTREE_SCHEMA = {
    "name": "remove_worktree",
    "description": "Remove a git worktree and its directory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Worktree name to remove.",
            },
        },
        "required": ["name"],
    },
}

KEEP_WORKTREE_SCHEMA = {
    "name": "keep_worktree",
    "description": "Mark a worktree as kept — it won't be auto-cleaned when the teammate shuts down.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Worktree name to keep.",
            },
        },
        "required": ["name"],
    },
}


# ── 工具 Handler ─────────────────────────────────────────────────────

def make_worktree_handlers(manager: WorktreeManager):
    """返回 3 个 worktree 工具的 handler"""

    def run_create_worktree(name: str, task_id: str | None = None) -> str:
        result = manager.create(name, task_id)
        if result.startswith("Error"):
            return result
        return f"Created worktree '{name}' at {result} (branch: wt/{name})"

    def run_remove_worktree(name: str) -> str:
        return manager.remove(name)

    def run_keep_worktree(name: str) -> str:
        return manager.keep(name)

    return {
        "create_worktree": run_create_worktree,
        "remove_worktree": run_remove_worktree,
        "keep_worktree": run_keep_worktree,
    }
