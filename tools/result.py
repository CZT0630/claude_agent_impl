"""Minimal structured result object for tool execution.

s22 keeps the current text-based agent loop compatible while giving tests,
audit, and future runtime events a stable shape to assert against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    artifact_refs: list[str] = field(default_factory=list)

    @classmethod
    def success(
        cls,
        stdout: str = "",
        *,
        stderr: str = "",
        metadata: dict[str, Any] | None = None,
        artifact_refs: list[str] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=True,
            stdout=stdout,
            stderr=stderr,
            metadata=metadata or {},
            artifact_refs=artifact_refs or [],
        )

    @classmethod
    def failure(
        cls,
        error_code: str,
        *,
        stderr: str = "",
        stdout: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            stdout=stdout,
            stderr=stderr,
            error_code=error_code,
            metadata=metadata or {},
        )

    @classmethod
    def from_output(
        cls,
        output: object,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        if isinstance(output, ToolResult):
            return output

        text = "" if output is None else str(output)
        if text.startswith("Error:"):
            return cls.failure("TOOL_ERROR_TEXT", stderr=text, metadata=metadata)
        return cls.success(stdout=text, metadata=metadata)

    def to_text(self) -> str:
        """Render the result for the existing Anthropic tool_result content."""
        if self.ok:
            text = (self.stdout or "") + (self.stderr or "")
            return text if text else "(no output)"

        detail = self.stderr or self.stdout or self.error_code or "tool failed"
        if detail.startswith("Error:"):
            return detail
        if self.error_code:
            return f"Error: {self.error_code}: {detail}"
        return f"Error: {detail}"
