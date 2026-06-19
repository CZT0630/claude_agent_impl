# Agent Runtime Threat Model

This document records the security baseline introduced in s22. It focuses on
the current local Python agent runtime after s21 command sandboxing.

## Assets

| Asset | Why It Matters | Baseline Control |
|-------|----------------|------------------|
| API keys and tokens | Model, GitHub, cloud, package, and database credentials can be exfiltrated by tool calls. | `clean_env()` removes known sensitive names and common sensitive suffixes. |
| Workspace files | The agent can read, write, and execute code in the project directory. | Permission checks and sandbox execution constrain high-risk commands. |
| Host machine | Shell commands run with the current user account unless isolated. | `Sandbox.execute()` is the only bash execution path for foreground and background bash. |
| Runtime history | Messages and tool outputs may contain private code or secrets. | s22 keeps structured `ToolResult` errors; s25 will persist auditable records. |
| Network boundary | Future web tools may access internal services or metadata endpoints. | SSRF and domain policy are required before adding web fetch/search. |

## Threats And Required Controls

| Threat | Example | Required Baseline |
|--------|---------|-------------------|
| Command injection | A prompt asks the agent to run destructive shell commands. | Deny list checks run before command execution and return `SANDBOX_DENY_PATTERN`. |
| Background bypass | `run_in_background=true` avoids the foreground sandbox. | Background bash must receive `executor=sandbox.execute`. |
| Secret leakage | A command prints `ANTHROPIC_API_KEY` or `GITHUB_TOKEN`. | Foreground and background bash must run with `clean_env()`. |
| Resource exhaustion | Fork bombs, infinite loops, large stdout, large files. | Sandbox strategies apply time/resource limits where possible; outputs are capped at 50,000 chars. |
| Tool exception ambiguity | A handler raises and the caller cannot distinguish failure from normal text. | `ToolRegistry.execute_result()` returns `ToolResult` with `TOOL_EXCEPTION`. |
| Unknown tool execution | The model calls an unregistered tool name. | Registry returns `UNKNOWN_TOOL` as a structured failure. |
| SSRF and internal network access | Future `web_fetch` calls `localhost` or cloud metadata IPs. | Web tools must not be added before Phase s27 network policy. |
| Prompt/log secret exposure | Tool errors include credentials in logs or prompts. | Future event/log layers must redact secrets before persistence or display. |

## s22 Security Tests

The s22 baseline is verified by:

```powershell
D:\Language\anaconda3\envs\tacn\python.exe -m pytest tests/security tests/runtime
```

The tests intentionally use `SANDBOX_LEVEL=off` equivalents for some cases.
That does not disable the baseline being tested: the fallback path still must
clean environment variables, enforce the deny list, and cap outputs.

## Non-Goals

- This phase does not implement multi-tenant authorization.
- This phase does not implement SSRF protection for web tools.
- This phase does not replace the ReAct loop with `LoopState`.
- This phase does not persist audit events to a database.

Those items belong to s23-s31. s22 only makes sure the current runtime has a
repeatable security and testing baseline before larger refactors begin.
