from __future__ import annotations

import sys

from tools.registry import ToolRegistry
from tools.result import ToolResult
from tools.sandbox import Sandbox


def _python_command(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def test_tool_result_success_and_failure_text_rendering():
    assert ToolResult.success("hello").to_text() == "hello"

    failure = ToolResult.failure("EXAMPLE_ERROR", stderr="bad input")
    assert failure.to_text() == "Error: EXAMPLE_ERROR: bad input"


def test_registry_unknown_tool_returns_structured_error():
    registry = ToolRegistry()

    result = registry.execute_result("missing_tool", {})

    assert not result.ok
    assert result.error_code == "UNKNOWN_TOOL"
    assert result.metadata["tool"] == "missing_tool"
    assert registry.execute("missing_tool", {}).startswith("Error:")


def test_registry_handler_exception_returns_structured_error():
    registry = ToolRegistry()

    def explode():
        raise RuntimeError("boom")

    registry.register("explode", "raise an error", {}, explode)
    result = registry.execute_result("explode", {})

    assert not result.ok
    assert result.error_code == "TOOL_EXCEPTION"
    assert result.stderr == "boom"
    assert result.metadata["exception_type"] == "RuntimeError"


def test_registry_accepts_handler_returning_tool_result():
    registry = ToolRegistry()
    registry.register(
        "structured",
        "return a structured result",
        {},
        lambda: ToolResult.success("already structured", metadata={"source": "test"}),
    )

    result = registry.execute_result("structured", {})

    assert result.ok
    assert result.stdout == "already structured"
    assert result.metadata == {"source": "test"}


def test_sandbox_structured_result_truncates_large_output(tmp_path):
    result = Sandbox(tmp_path, level="off").execute_result(
        _python_command("print('x' * 60000)"),
        timeout=10,
    )

    assert result.ok
    assert len(result.stdout) == 50000
