---
name: agent-builder
description: Build and extend the Claude Agent project following its architecture.
---

# Agent Builder Skill

This skill helps you extend the Claude Agent project.

## Architecture

```
Agent = LLM + Harness (tools + hooks + permissions)
```

The core loop: `while stop_reason == "tool_use": execute tools → feed results back`

## Adding a New Tool

1. Create `tools/your_tool.py`:
   ```python
   YOUR_SCHEMA = {
       "name": "your_tool",
       "description": "What it does.",
       "input_schema": { "type": "object", "properties": {...}, "required": [...] },
   }

   def make_your_handler(workdir: Path):
       def run_your_tool(param: str) -> str:
           # implementation
           return "result"
       return run_your_tool
   ```

2. Register in `main.py`:
   ```python
   from tools.your_tool import YOUR_SCHEMA, make_your_handler
   registry.register(**YOUR_SCHEMA, handler=make_your_handler(config.workdir))
   ```

## Adding a New Hook

```python
hooks.register("PreToolUse", my_hook)      # before tool execution
hooks.register("PostToolUse", my_hook)      # after tool execution
hooks.register("UserPromptSubmit", my_hook) # before LLM call
hooks.register("Stop", my_hook)             # when loop exits
```

Hook returning non-None blocks execution.

## Module Integration Pattern

Each new module follows this pattern:
1. Create module file (e.g., `skills/loader.py`)
2. Export a schema (for tools) or a class (for services)
3. Wire it up in `main.py`'s `build_agent()`
