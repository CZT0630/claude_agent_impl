from __future__ import annotations

import re
import sys
import time

from tools.background import BackgroundManager
from tools.bash import make_bash_handler
from tools.sandbox import Sandbox, clean_env


def _python_command(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def _collect_until(manager: BackgroundManager, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        notifications = manager.collect()
        if notifications:
            return "\n".join(notifications)
        time.sleep(0.05)
    raise AssertionError(f"background task did not finish: {manager.status()}")


def test_clean_env_removes_sensitive_names_and_suffixes(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-1")
    monkeypatch.setenv("CUSTOM_SERVICE_TOKEN", "secret-2")
    monkeypatch.setenv("SAFE_PUBLIC_VALUE", "visible")

    env = clean_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "CUSTOM_SERVICE_TOKEN" not in env
    assert env["SAFE_PUBLIC_VALUE"] == "visible"


def test_sandbox_deny_list_has_structured_error(tmp_path):
    result = Sandbox(tmp_path, level="off").execute_result("rm -rf /")

    assert not result.ok
    assert result.error_code == "SANDBOX_DENY_PATTERN"
    assert "rm -rf /" in result.stderr
    assert result.to_text().startswith("Error:")


def test_foreground_bash_uses_sandbox_env_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "S22_SHOULD_NOT_LEAK")
    handler = make_bash_handler(tmp_path, sandbox_level="off")

    output = handler(
        _python_command("import os; print(os.getenv('ANTHROPIC_API_KEY', 'missing'))")
    )

    assert "missing" in output
    assert "S22_SHOULD_NOT_LEAK" not in output


def test_background_bash_uses_same_sandbox_executor(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "S22_BACKGROUND_SECRET")
    manager = BackgroundManager(tmp_path)
    handler = make_bash_handler(tmp_path, bg_manager=manager, sandbox_level="off")

    started = handler(
        _python_command("import os; print(os.getenv('ANTHROPIC_API_KEY', 'missing'))"),
        run_in_background=True,
    )
    assert re.search(r"bg_\d+_\d{4}", started)

    notification = _collect_until(manager)
    assert "status='completed'" in notification
    assert "missing" in notification
    assert "S22_BACKGROUND_SECRET" not in notification


def test_background_dangerous_command_is_failed_by_sandbox(tmp_path):
    manager = BackgroundManager(tmp_path)
    handler = make_bash_handler(tmp_path, bg_manager=manager, sandbox_level="off")

    handler("rm -rf /", run_in_background=True)
    notification = _collect_until(manager)

    assert "status='failed'" in notification
    assert "Error:" in notification
    assert "rm -rf /" in notification
