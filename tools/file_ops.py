"""
文件操作工具 — read / write / edit / glob
"""

import glob as glob_module
from pathlib import Path


def _safe_path(workdir: Path, p: str) -> Path:
    path = (workdir / p).resolve()
    if not path.is_relative_to(workdir.resolve()):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


READ_SCHEMA = {
    "name": "read_file",
    "description": "Read file contents.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["path"],
    },
}

WRITE_SCHEMA = {
    "name": "write_file",
    "description": "Write content to a file.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
}

EDIT_SCHEMA = {
    "name": "edit_file",
    "description": "Replace exact text in a file once.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
        "required": ["path", "old_text", "new_text"],
    },
}

GLOB_SCHEMA = {
    "name": "glob",
    "description": "Find files matching a glob pattern.",
    "input_schema": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    },
}


def make_read_handler(workdir: Path):
    def run_read(path: str, limit: int | None = None) -> str:
        try:
            lines = _safe_path(workdir, path).read_text(encoding="utf-8").splitlines()
            if limit and limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"
    return run_read


def make_write_handler(workdir: Path):
    def run_write(path: str, content: str) -> str:
        try:
            file_path = _safe_path(workdir, path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error: {e}"
    return run_write


def make_edit_handler(workdir: Path):
    def run_edit(path: str, old_text: str, new_text: str) -> str:
        try:
            file_path = _safe_path(workdir, path)
            text = file_path.read_text(encoding="utf-8")
            if old_text not in text:
                return f"Error: text not found in {path}"
            file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
            return f"Edited {path}"
        except Exception as e:
            return f"Error: {e}"
    return run_edit


def make_glob_handler(workdir: Path):
    def run_glob(pattern: str) -> str:
        try:
            results = []
            for match in glob_module.glob(pattern, root_dir=workdir, recursive=True):
                if (workdir / match).resolve().is_relative_to(workdir.resolve()):
                    results.append(match)
            return "\n".join(results) if results else "(no matches)"
        except Exception as e:
            return f"Error: {e}"
    return run_glob
