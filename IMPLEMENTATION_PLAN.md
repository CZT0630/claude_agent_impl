# Claude Agent 完整实现方案

> 基于 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 仓库的 20 章递进式课程

## 核心理念

```
Agent = 模型(LLM) + Harness(工具+知识+上下文+权限)
```

**你不是在写智能，你是在造载具。** 模型负责推理，Harness 负责给模型双手、双眼和工作空间。

```
THE AGENT PATTERN
=================

User --> messages[] --> LLM --> response
                                  |
                        stop_reason == "tool_use"?
                       /                          \
                     yes                           no
                      |                             |
                execute tools                    return text
                append results
                loop back -----------------> messages[]
```

## 技术栈

| 项目 | 选型 |
|------|------|
| 语言 | Python 3.10+ |
| LLM SDK | `anthropic` (兼容 DeepSeek/GLM/Kimi/MiniMax) |
| 配置 | `python-dotenv` |
| 技能解析 | `pyyaml` |
| 模型 | Claude Sonnet 4.6 (或兼容模型) |

```bash
pip install anthropic python-dotenv pyyaml
```

## .env 配置模板

```env
# API Key (必填)
ANTHROPIC_API_KEY=sk-ant-xxx

# 模型 ID (必填)
MODEL_ID=claude-sonnet-4-6

# 可选: 兼容提供商的 Base URL
# ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
# MODEL_ID=deepseek-chat
```

---

## 九大阶段 · 28 个模块 总览

```
阶段一: 让 Agent 能动手          s01 → s02 → s03 → s04
阶段二: 做复杂任务              s05 → s06 → s07 → s08
阶段三: 记住和恢复              s09 → s10 → s11
阶段四: 让任务长期运行           s12 → s13 → s14
阶段五: 让多个 Agent 协作        s15 → s16 → s17 → s18
阶段六: 接外部能力合体           s19 → s20
阶段七: 安全加固                 s21 → s22
阶段八: 工具生态扩展             s23 → s24 → s25
阶段九: 会话与交互               s26 → s27 → s28
```

---

## 阶段一：让 Agent 能动手（s01-s04）

### s01 Agent Loop — "一个循环 + Bash 就够了"

**目标**: 实现最小可用的 agent 循环

**核心代码 (~30 行有效逻辑)**:
```python
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]

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
            if block.type == "tool_use":
                output = run_bash(block.input["command"])
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
        messages.append({"role": "user", "content": results})
```

**关键概念**:
- `messages` 列表是整个 agent 的状态
- `stop_reason == "tool_use"` 控制循环继续
- 工具结果以 `user` 角色反馈给模型

**新增文件**: `s01_agent_loop/code.py`

---

### s02 Tool Use — "加一个工具，只加一个 handler"

**目标**: 从 1 个工具扩展到 5 个，用分发映射替代硬编码

**新增工具**:
| 工具名 | 功能 |
|--------|------|
| `bash` | 执行 shell 命令 (来自 s01) |
| `read_file` | 读取文件内容 |
| `write_file` | 写入文件 |
| `edit_file` | 精确替换文件中的文本 |
| `glob` | 按模式查找文件 |

**关键增量 — `TOOL_HANDLERS` 分发映射**:
```python
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}

# agent_loop 中只需改一行:
handler = TOOL_HANDLERS.get(block.name)
output = handler(**block.input) if handler else f"Unknown: {block.name}"
```

**安全路径校验**:
```python
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
```

**关键概念**:
- 循环本身不变，只改工具执行部分
- 新工具 = 新函数 + 新 schema + 注册到 TOOL_HANDLERS
- `safe_path` 防止路径穿越攻击

**新增文件**: `s02_tool_use/code.py`

---

### s03 Permission — "先划边界，再给自由"

**目标**: 在工具执行前插入三级权限检查

**三级权限管线**:
```
Gate 1: DENY_LIST 硬拒绝
    → rm -rf /, sudo, shutdown, reboot, mkfs, dd if=, > /dev/sda
    → 命中即阻断，无需用户确认

Gate 2: PERMISSION_RULES 规则匹配
    → 写工作区外的文件？
    → 执行破坏性命令 (rm, > /etc/, chmod 777)？
    → 命中则进入 Gate 3

Gate 3: ask_user() 用户审批
    → 打印警告信息
    → 等待用户输入 y/N
    → 用户拒绝则阻断
```

**核心代码**:
```python
def check_permission(block) -> bool:
    # Gate 1
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"⛔ {reason}")
            return False
    # Gate 2
    reason = check_rules(block.name, block.input)
    if reason:
        # Gate 3
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return False
    return True
```

**关键概念**:
- agent_loop 中只加一行: `if not check_permission(block): continue`
- 权限被拒绝时返回 `"Permission denied."` 作为 tool_result

**新增文件**: `s03_permission/code.py`

---

### s04 Hooks — "挂在循环上，不写进循环里"

**目标**: 把扩展逻辑从循环体移出，放到 hook 注册表上

**Hook 注册表**:
```python
HOOKS = {
    "UserPromptSubmit": [],  # 用户输入后、LLM 调用前
    "PreToolUse": [],        # 工具执行前
    "PostToolUse": [],       # 工具执行后
    "Stop": [],              # 循环即将退出时
}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:  # 返回非 None = 阻断
            return result
    return None
```

**内置 Hooks**:
| Hook | 事件 | 功能 |
|------|------|------|
| `permission_hook` | PreToolUse | s03 的权限逻辑迁移到这里 |
| `log_hook` | PreToolUse | 记录每次工具调用 |
| `large_output_hook` | PostToolUse | 大输出警告 |
| `context_inject_hook` | UserPromptSubmit | 注入工作目录信息 |
| `summary_hook` | Stop | 打印工具调用次数 |

**关键概念**:
- s03 的 `check_permission()` 被 `trigger_hooks("PreToolUse", block)` 替代
- 循环体保持干净，所有扩展通过 hook 注入
- hook 返回非 None 值可阻断工具执行

**新增文件**: `s04_hooks/code.py`

---

## 阶段二：做复杂任务（s05-s08）

### s05 TodoWrite — "先列计划再动手"

**目标**: 添加计划工具，让 agent 先规划后执行

**新增工具 `todo_write`**:
```python
{
    "name": "todo_write",
    "description": "Create and manage a task list.",
    "input_schema": {
        "todos": [{
            "content": str,           # 任务描述
            "status": "pending" | "in_progress" | "completed"
        }]
    }
}
```

**Nag Reminder 机制**:
```python
rounds_since_todo = 0

def agent_loop(messages):
    global rounds_since_todo
    while True:
        # 3 轮没更新 todo 就注入提醒
        if rounds_since_todo >= 3:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        # ... 正常循环 ...

        if block.name == "todo_write":
            rounds_since_todo = 0  # 调用 todo_write 时重置计数
```

**关键概念**:
- todo 状态保存在内存中 (`CURRENT_TODOS`)
- SYSTEM prompt 加入 "plan before execute" 引导
- nag reminder 确保 agent 不会忘记更新计划

**新增文件**: `s05_todo_write/code.py`

---

### s06 Subagent — "大任务拆小，干净上下文"

**目标**: 用全新 `messages[]` 派生子 agent，只返回摘要

**子 Agent 架构**:
```
Parent Agent                     Subagent
+------------------+            +------------------+
| messages=[...]   |            | messages=[task]  | ← 全新上下文
|                  |  dispatch  |                  |
| tool: task       | ---------> | own while loop   |
|   prompt="..."   |            |   bash/read/...  |
|                  |  summary   |   (max 30 turns) |
| result = "..."   | <--------- | return last text |
+------------------+            +------------------+
                                          |
                          intermediate results DISCARDED
```

**核心代码**:
```python
SUB_SYSTEM = "Complete the task, then return a concise summary. Do not delegate further."

def spawn_subagent(description: str) -> str:
    messages = [{"role": "user", "content": description}]  # 全新上下文

    for _ in range(30):  # 安全限制
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        # ... 执行工具 ...

    return extract_text(messages[-1]["content"])  # 只返回摘要，历史丢弃
```

**关键概念**:
- 子 agent 没有 `task` 工具 → 不能递归派生
- 子 agent 有 30 轮安全限制
- 中间结果丢弃，只带回最终摘要 → 上下文隔离

**新增文件**: `s06_subagent/code.py`

---

### s07 Skill Loading — "用到时再加载"

**目标**: 两级按需知识注入，避免塞满 prompt

**两级加载策略**:
```
Layer 1 (便宜，始终在):
  SYSTEM prompt 注入技能目录 (~100 tokens/skill)
  "Skills available: agent-builder, code-review, mcp-builder, pdf"

Layer 2 (昂贵，按需):
  Agent 调用 load_skill("code-review") → 完整 SKILL.md 内容
  通过 tool_result 注入 (~2000 tokens/skill)
```

**技能目录结构**:
```
skills/
  agent-builder/SKILL.md    ← YAML frontmatter + markdown 内容
  code-review/SKILL.md
  mcp-builder/SKILL.md
  pdf/SKILL.md
```

**核心代码**:
```python
SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    """启动时扫描 skills/ 目录，构建注册表"""
    for d in sorted(SKILLS_DIR.iterdir()):
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw)
            SKILL_REGISTRY[meta.get("name", d.name)] = {
                "name": meta.get("name", d.name),
                "description": meta.get("description", ""),
                "content": raw,
            }

def build_system() -> str:
    """SYSTEM 包含技能目录（便宜）"""
    catalog = list_skills()
    return f"You are a coding agent.\nSkills available:\n{catalog}\nUse load_skill to get full details."

def load_skill(name: str) -> str:
    """按需加载完整内容（昂贵）"""
    skill = SKILL_REGISTRY.get(name)
    return skill["content"] if skill else f"Skill not found: {name}"
```

**关键概念**:
- 启动时扫描一次，构建 `SKILL_REGISTRY`
- SYSTEM 只放目录（名字+描述），不放全文
- 模型需要时主动调用 `load_skill` 获取全文

**新增文件**: `s07_skill_loading/code.py`, `skills/` 目录

---

### s08 Context Compact — "上下文总会满"

**目标**: 四层压缩管线，便宜的先跑贵的后跑

**压缩管线架构**:
```
messages[]
    ↓
L3 budget → L1 snip → L2 micro → [token > threshold?]
                                   ├─ No  → LLM
                                   └─ Yes → L4 summary → LLM
                                                         ↓
                                                   [prompt_too_long?]
                                                     └─ Yes → reactive compact
```

**四层实现**:

| 层级 | 名称 | 成本 | 策略 |
|------|------|------|------|
| L1 | `snip_compact` | 零 API | 消息数 > 50 时裁剪中间消息 |
| L2 | `micro_compact` | 零 API | 旧 tool_result 替换为占位符 |
| L3 | `tool_result_budget` | 零 API | 大输出持久化到磁盘 |
| L4 | `compact_history` | 1 次 API | LLM 全文摘要 |
| Emergency | `reactive_compact` | 1 次 API | API 返回 prompt_too_long 时触发 |

**L1: snip_compact**:
```python
def snip_compact(messages, max_messages=50):
    if len(messages) <= max_messages:
        return messages
    keep_head, keep_tail = 3, max_messages - 3
    snipped = len(messages) - keep_head - keep_tail
    return messages[:keep_head] + \
           [{"role": "user", "content": f"[snipped {snipped} messages]"}] + \
           messages[-keep_tail:]
```

**L2: micro_compact**:
```python
def micro_compact(messages):
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT:
        return messages
    for _, _, block in tool_results[:-KEEP_RECENT]:
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages
```

**L3: tool_result_budget**:
```python
def persist_large_output(tool_use_id, output):
    if len(output) <= PERSIST_THRESHOLD:
        return output
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    path.write_text(output)
    return f"<persisted-output>\nFull: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"
```

**L4: compact_history**:
```python
def compact_history(messages):
    write_transcript(messages)  # 先保存完整记录
    summary = summarize_history(messages)  # LLM 摘要
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]
```

**关键概念**:
- 执行顺序匹配 Claude Code 源码: budget → snip → micro → auto
- 压缩前保存 transcript 到 `.transcripts/`
- 大输出持久化到 `.task_outputs/tool-results/`

**新增文件**: `s08_context_compact/code.py`

---

## 阶段三：记住和恢复（s09-s11）

### s09 Memory — "记住该记的，忘掉该忘的"

**目标**: 跨会话持久记忆，三个子系统协作

**存储结构**:
```
.memory/
  MEMORY.md          ← 索引（每行一条，≤200 行）
  user-profile.md    ← 单条记忆（YAML frontmatter + markdown）
  project-facts.md
  feedback-tabs.md
```

**记忆文件格式**:
```markdown
---
name: user-profile
description: 用户偏好和角色信息
type: user
---

用户是一名初学者，正在学习实现 Claude Agent...
```

**三个子系统**:

| 子系统 | 时机 | 功能 |
|--------|------|------|
| Selection | LLM 调用前 | 按相关性选择记忆注入上下文 |
| Extraction | 每轮结束后 | 从对话中提取新记忆 |
| Consolidation | 记忆数 ≥ 10 | 合并去重、删除过时记忆 |

**Selection (选择)**:
```python
def select_relevant_memories(messages, max_items=5):
    # 收集最近 3 条用户消息
    # 用 LLM 从目录中选择相关记忆的索引
    # fallback: 关键词匹配
    return selected_filenames
```

**Extraction (提取)**:
```python
def extract_memories(messages):
    # 收集最近 10 条消息
    # 用 LLM 提取用户偏好、约束、项目事实
    # 返回 JSON 数组: [{name, type, description, body}]
    # 写入 .memory/{slug}.md
```

**Consolidation (整理)**:
```python
def consolidate_memories():
    # 当记忆文件 ≥ 10 个时触发
    # 用 LLM 合并重复、删除过时记忆
    # 保持总数 ≤ 30
```

**关键概念**:
- MEMORY.md 索引始终注入 SYSTEM prompt（便宜）
- 相关记忆内容按需注入用户消息（中等）
- 压缩前保存快照，确保记忆提取的完整性

**新增文件**: `s09_memory/code.py`, `.memory/` 目录

---

### s10 System Prompt — "prompt 是组装出来的"

**目标**: 运行时动态组装 SYSTEM prompt，按上下文条件加载段落

**Prompt 段落注册表**:
```python
PROMPT_SECTIONS = {
    "identity":  "You are a coding agent. Act, don't explain.",
    "tools":     "Available tools: bash, read_file, write_file.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory":    "Relevant memories are injected below when available.",
    "skills":    "Use load_skill to get full skill details.",
}
```

**条件组装**:
```python
def assemble_system_prompt(context: dict) -> str:
    sections = []
    sections.append(PROMPT_SECTIONS["identity"])     # 始终加载
    sections.append(PROMPT_SECTIONS["tools"])         # 始终加载
    sections.append(PROMPT_SECTIONS["workspace"])     # 始终加载

    if context.get("memories"):                       # 有条件加载
        sections.append(PROMPT_SECTIONS["memory"])

    if SKILL_REGISTRY:                                # 有条件加载
        sections.append(PROMPT_SECTIONS["skills"])

    return "\n\n".join(sections)
```

**缓存机制**:
```python
_prompt_cache = {}

def get_system_prompt(context: dict) -> str:
    key = json.dumps(context, sort_keys=True, default=str)
    if key not in _prompt_cache:
        _prompt_cache[key] = assemble_system_prompt(context)
    return _prompt_cache[key]
```

**关键概念**:
- SYSTEM prompt 不再是硬编码字符串
- 按实际状态（是否有记忆、是否有技能）条件加载
- 相同上下文 → 缓存命中 → 避免重复组装

**新增文件**: `s10_system_prompt/code.py`

---

### s11 Error Recovery — "错误是重试的起点"

**目标**: 三种错误恢复路径 + 指数退避

**三条恢复路径**:

```
Path 1: max_tokens → 升级 8K→64K → continuation prompt（最多 3 次）
Path 2: prompt_too_long → reactive compact → 重试（1 次）
Path 3: 429/529 → 指数退避 + jitter（最多 10 次）→ fallback 模型
```

**Path 1: Token 升级**:
```python
class RecoveryState:
    escalated = False       # 是否已升级 max_tokens
    continuation_count = 0  # continuation prompt 次数
    consecutive_529 = 0     # 连续 529 次数

# stop_reason == "max_tokens" 时:
if not state.escalated:
    max_tokens = ESCALATED_MAX_TOKENS  # 8K → 64K
    state.escalated = True
else:
    messages.append({"role": "user", "content": CONTINUATION_PROMPT})
    state.continuation_count += 1
```

**Path 2: Prompt 过长**:
```python
except Exception as e:
    if "prompt_too_long" in str(e).lower():
        messages[:] = reactive_compact(messages)  # 压缩后重试
```

**Path 3: 速率限制 + 指数退避**:
```python
def backoff_delay(attempt):
    delay_ms = BASE_DELAY_MS * (2 ** attempt)
    jitter = random.uniform(0, delay_ms * 0.5)
    return (delay_ms + jitter) / 1000

# 429/529 时:
time.sleep(backoff_delay(retry_count))
# 连续 529 超过阈值 → 切换 fallback 模型
```

**关键概念**:
- `RecoveryState` 跟踪恢复状态
- 每种错误有独立的恢复路径
- 指数退避防止 API 过载

**新增文件**: `s11_error_recovery/code.py`

---

## 阶段四：让任务长期运行（s12-s14）

### s12 Task System — "大目标拆小任务，持久化"

**目标**: 文件持久化的任务图，支持依赖关系

**任务数据结构**:
```python
@dataclass
class Task:
    id: str                   # task_1234567890_0001
    subject: str              # 任务标题
    description: str          # 详细描述
    status: str               # pending | in_progress | completed
    owner: str | None         # Agent 名字 (多 agent 场景)
    blockedBy: list[str]      # 依赖的任务 ID 列表
```

**存储**: `.tasks/{task_id}.json`

**5 个新工具**:
| 工具 | 功能 |
|------|------|
| `create_task` | 创建任务，可指定依赖 |
| `list_tasks` | 列出所有任务 |
| `get_task` | 获取单个任务详情 |
| `claim_task` | 认领任务 (设置 owner) |
| `complete_task` | 完成任务 |

**依赖检查**:
```python
def can_start(task: Task) -> bool:
    """检查所有依赖是否已完成"""
    for dep_id in task.blockedBy:
        dep = load_task(dep_id)
        if not dep or dep.status != "completed":
            return False
    return True
```

**关键概念**:
- 任务持久化到磁盘，跨会话存活
- `blockedBy` 实现任务依赖图
- 为后续多 agent 协作奠定基础

**新增文件**: `s12_task_system/code.py`, `.tasks/` 目录

---

### s13 Background Tasks — "慢操作丢后台"

**目标**: 用线程实现异步执行，完成后注入通知

**后台任务架构**:
```
agent_loop
    ↓ (检测到慢操作)
start_background_task()
    → threading.Thread(daemon=True).start()
    → 返回 bg_id 作为占位符
    ↓ (继续思考其他事)
    ↓
collect_background_results()
    → 检查完成的后台任务
    → 注入 <task_notification> 到 messages
```

**核心代码**:
```python
background_tasks = {}      # bg_id → {command, status, thread}
background_results = {}    # bg_id → output (thread-safe)
results_lock = threading.Lock()

def start_background_task(command, bg_id):
    def run():
        output = run_bash(command)
        with results_lock:
            background_results[bg_id] = output
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    background_tasks[bg_id] = {"command": command, "status": "running", "thread": thread}

def collect_background_results():
    notifications = []
    for bg_id, result in list(background_results.items()):
        notifications.append(f"<task_notification id='{bg_id}'>\n{result}\n</task_notification>")
        del background_results[bg_id]
    return notifications
```

**关键概念**:
- 模型可以通过 `run_in_background` 参数显式请求后台执行
- 也可以通过 `is_slow_operation` 启发式自动判断
- 通知使用 `<task_notification>` 格式注入

**新增文件**: `s13_background_tasks/code.py`

---

### s14 Cron Scheduler — "按时自动触发"

**目标**: 独立守护线程 + 队列处理器，实现定时任务

**四层架构**:
```
Layer 1: Scheduler (守护线程)
    → 每秒检查 cron 表达式
    → 匹配时写入 cron_queue

Layer 2: Queue (线程安全队列)
    → cron_queue 解耦调度器和 agent

Layer 3: Queue Processor
    → 有工作时唤醒 agent

Layer 4: Consumer (agent_loop)
    → 消费队列中的任务，注入 messages
```

**Cron 表达式匹配**:
```python
def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """5 字段 cron 表达式: 分 时 日 月 周"""
    parts = cron_expr.split()
    # 每个字段匹配: * 或具体值 或 逗号分隔列表
    # DOM/DOW 使用 OR 语义 (与标准 cron 一致)
```

**新工具**:
| 工具 | 功能 |
|------|------|
| `schedule_cron` | 注册定时任务 |
| `list_crons` | 列出所有定时任务 |
| `cancel_cron` | 取消定时任务 |

**持久化**: `.scheduled_tasks.json`

**关键概念**:
- 调度器独立于 agent loop 运行
- 队列解耦确保调度器不阻塞 agent
- 持久化确保重启后恢复调度

**新增文件**: `s14_cron_scheduler/code.py`

---

## 阶段五：让多个 Agent 协作（s15-s18）

### s15 Agent Teams — "一个搞不定，组队来"

**目标**: 消息总线 + 文件邮箱 + 异步队友

**架构**:
```
Lead Agent                          Teammate Agent
+------------------+               +------------------+
| messages=[...]   |               | messages=[task]  |
|                  |  MessageBus   |                  |
| spawn_teammate   | ────────────> | own agent_loop   |
| send_message     | ────────────> |   bash/read/write|
| check_inbox      | <──────────── |   send_message   |
+------------------+               +------------------+
        ↑                                   |
        └── inbox ← .mailboxes/teammate.jsonl
```

**MessageBus 实现**:
```python
class MessageBus:
    MAILBOX_DIR = ".mailboxes"

    def send(self, to: str, msg_type: str, payload: dict):
        mailbox = self.MAILBOX_DIR / f"{to}.jsonl"
        entry = {"type": msg_type, "payload": payload, "timestamp": time.time()}
        with mailbox.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def receive(self, agent_name: str) -> list[dict]:
        mailbox = self.MAILBOX_DIR / f"{agent_name}.jsonl"
        if not mailbox.exists():
            return []
        messages = [json.loads(line) for line in mailbox.read_text().splitlines() if line.strip()]
        mailbox.write_text("")  # 清空
        return messages
```

**新工具 (Lead)**:
| 工具 | 功能 |
|------|------|
| `spawn_teammate` | 派生队友线程 |
| `send_message` | 向队友发送消息 |
| `check_inbox` | 检查自己的收件箱 |

**关键概念**:
- 队友在独立线程中运行自己的 agent_loop
- 通过文件邮箱异步通信
- 队友有 10 轮安全限制

**新增文件**: `s15_agent_teams/code.py`, `.mailboxes/` 目录

---

### s16 Team Protocols — "队友之间要有约定"

**目标**: 请求-回复协议 + 状态机

**协议状态机**:
```
ProtocolState:
  request_id: str       # 请求唯一 ID
  type: str             # shutdown | plan_approval
  sender: str           # 发送者
  status: str           # pending → approved/rejected
  created_at: float
```

**协议流程 (以关机为例)**:
```
Lead: BUS.send("shutdown_request", {request_id})
  → Teammate inbox
  → dispatch → handler
  → BUS.send("shutdown_response", {request_id, approved: true})
  → Lead inbox
  → match_response(request_id) → status = approved
```

**新工具 (Lead)**:
| 工具 | 功能 |
|------|------|
| `request_shutdown` | 发送关机请求 |
| `request_plan` | 请求队友提交计划 |
| `review_plan` | 审批队友计划 |

**关键概念**:
- `request_id` 关联请求和响应
- `dispatch_message` 按消息类型路由到处理器
- 队友进入 idle loop 等待消息，不再 10 轮后退出

**新增文件**: `s16_team_protocols/code.py`

---

### s17 Autonomous Agents — "自己看板认领活"

**目标**: WORK/IDLE 生命周期 + 自动认领任务

**队友生命周期**:
```
WORK: inbox → LLM → tools → (tool_use? loop) → (done? → IDLE)
IDLE: 5s poll → inbox? → WORK / unclaimed? → claim → WORK / 60s? → SHUTDOWN
```

**自动认领**:
```python
def scan_unclaimed_tasks():
    """查找未认领的、依赖已完成的 pending 任务"""
    tasks = list_tasks()
    return [t for t in tasks
            if t.status == "pending"
            and t.owner is None
            and can_start(t)]
```

**新增队友工具**:
| 工具 | 功能 |
|------|------|
| `list_tasks` | 查看任务列表 |
| `claim_task` | 认领任务 |
| `complete_task` | 完成任务 |

**关键概念**:
- 队友不再需要 Lead 逐个分配任务
- 空闲时自动扫描任务看板
- 60 秒无活可干则自动关机

**实现**: `scan_unclaimed_tasks` + `auto_claim_task` 已合并入 `tools/task.py`，`tools/teams.py` 直接调用。`s17_autonomous_agents/` 目录已删除。

---

### s18 Worktree Isolation — "各干各的目录"

**目标**: git worktree 隔离 + 任务-目录绑定

**工作区拓扑**:
```
Main repo (/)
  ├── .worktrees/auth/  (branch: wt/auth)  ← Task #1
  ├── .worktrees/ui/    (branch: wt/ui)     ← Task #2
  ├── .tasks/task_xxx.json (worktree: "auth")
  └── .worktrees/events.jsonl
```

**核心操作**:
```python
def create_worktree(name: str, task_id: str | None = None):
    validate_worktree_name(name)  # 防路径穿越
    run_git(["worktree", "add", f".worktrees/{name}", "-b", f"wt/{name}"])
    if task_id:
        bind_task_to_worktree(task_id, name)

def bind_task_to_worktree(task_id: str, worktree_name: str):
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)
```

**事件日志**: `.worktrees/events.jsonl`

**新工具 (Lead)**:
| 工具 | 功能 |
|------|------|
| `create_worktree` | 创建隔离工作区 |
| `remove_worktree` | 删除工作区 |
| `keep_worktree` | 保留工作区（不自动清理） |

**关键概念**:
- 每个任务绑定独立的 git worktree
- 队友在 worktree 目录中工作，互不干扰
- 事件日志记录所有操作

**实现**: `tools/worktree.py` (WorktreeManager + 3 个工具)，已集成到 `tools/teams.py` 的 `spawn()` 和 `main.py`。`s18_worktree_isolation/` 目录已废弃。

---

## 阶段六：接外部能力合体（s19-s20）

### s19 MCP Plugin — "能力不够？插上 MCP"

**目标**: MCP 客户端 + 工具发现 + 统一工具池

**MCP 架构**:
```
connect_mcp("docs") → MCPClient discovers tools →
assemble_tool_pool → [builtin..., mcp__docs__search, mcp__docs__get_version]
agent_loop uses assembled pool
```

**MCPClient 实现**:
```python
class MCPClient:
    def __init__(self, server_name: str):
        self.server_name = server_name
        self.tools = []

    def connect(self):
        """发现远程工具"""
        # 获取工具列表和 schema
        pass

    def call_tool(self, tool_name: str, args: dict) -> str:
        """调用远程工具"""
        pass
```

**工具池组装**:
```python
def assemble_tool_pool(builtin_tools, mcp_connections):
    pool = list(builtin_tools)
    for conn in mcp_connections:
        for tool in conn.tools:
            pool.append({
                "name": f"mcp__{conn.server_name}__{tool['name']}",
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            })
    return pool
```

**工具命名**: `mcp__{server}__{tool}`

**关键概念**:
- MCP 工具和内置工具统一到一个池
- 工具发现是自动的，不需要手动注册
- MCP 工具有 `readOnly`/`destructive` 标注

**新增文件**: `s19_mcp_plugin/code.py`

---

### s20 Comprehensive Agent — "机制很多，循环一个"

**目标**: 全部 19 个机制整合到一个 agent 中

**整合清单**:
```
✓ TOOL_HANDLERS 分发映射          (s02)
✓ Permission 三级权限管线         (s03)
✓ Hook 系统                      (s04)
✓ TodoWrite 计划工具              (s05)
✓ Subagent 子 agent              (s06)
✓ Skill Loading 两级加载          (s07)
✓ Context Compact 四层压缩        (s08)
✓ Memory 跨会话记忆               (s09)
✓ System Prompt 动态组装          (s10)
✓ Error Recovery 三条恢复路径     (s11)
✓ Task System 持久化任务图        (s12)
✓ Background Tasks 后台执行       (s13)
✓ Cron Scheduler 定时调度         (s14)
✓ Agent Teams 消息总线            (s15)
✓ Team Protocols 请求-回复协议    (s16)
✓ Autonomous Agents 自动认领      (s17)
✓ Worktree Isolation 目录隔离     (s18)
✓ MCP Plugin 外部工具集成         (s19)
```

**agent_loop 完整流程**:
```
1. collect_background_results()        ← s13
2. check cron_queue                    ← s14
3. load memories → inject              ← s09
4. assemble_system_prompt()            ← s10
5. tool_result_budget()                ← s08 L3
6. snip_compact()                      ← s08 L1
7. micro_compact()                     ← s08 L2
8. auto compact if needed              ← s08 L4
9. trigger_hooks("UserPromptSubmit")   ← s04
10. LLM call (with retry/backoff)      ← s11
11. trigger_hooks("Stop")              ← s04
12. for each tool_use:
    a. trigger_hooks("PreToolUse")     ← s04 (含 s03 权限)
    b. TOOL_HANDLERS[name](**input)    ← s02
    c. trigger_hooks("PostToolUse")    ← s04
13. extract_memories()                 ← s09
14. consolidate_memories()             ← s09
```

**新增文件**: `s20_comprehensive/code.py`

---

## 阶段七：安全加固（s21-s22）

### s21 Command Sandbox — "命令在笼子里跑"

**目标**: 在 `run_bash` 层面插入进程级隔离，Agent 即使被 prompt injection 操纵也无法破坏宿主机

**三级沙箱策略**:

```
Level 0 (当前): subprocess.run(shell=True) ← 宿主机裸跑

Level 1 (轻量): subprocess + 环境限制
    → env 清空敏感变量 (SSH_KEY, AWS_SECRET, ...)
    → 工作目录 chdir 锁定
    → resource 模块限制 CPU/内存/文件大小

Level 2 (容器): Docker 容器执行
    → docker run --rm -v workdir:/work -w /work
    → --network=none (可选禁网)
    → --memory=512m --cpus=1 --pids-limit=256
```

**架构**:

```
agent_loop
    ↓ tool_use: bash(command)
    ↓
Sandbox.execute(command)
    ├─ Level 0 → subprocess.run(shell=True)          ← 原始行为
    ├─ Level 1 → subprocess.run(env=clean_env, ...)   ← 受限子进程
    └─ Level 2 → docker run ... sh -c command         ← 容器隔离
```

**配置**: `SANDBOX_LEVEL=0|1|2` 通过 `.env` 或命令行参数控制

**关键概念**:
- 只改 `tools/bash.py` 的 `make_bash_handler`，将 `Sandbox.execute()` 替代 `subprocess.run()`，其他模块零改动
- Level 1 通过 `env` 清空 + `resource` 限制实现轻量隔离（无需 Docker）
- Level 2 通过 `--network=none` 实现网络隔离，`--memory` 防内存炸弹
- `preexec_fn=set_limits` 在子进程中设置资源限制，不影响主进程

**新增文件**: `s21_sandbox/code.py`, `s21_sandbox/Dockerfile`

---

### s22 Permission Modes — "该问的问，不该问的别问"

**目标**: 多级权限模式，平衡安全和效率

**三种模式**:

```
Mode 1: ask (当前行为)
  → 危险操作问用户 y/N

Mode 2: auto-accept (全允许)
  → 跳过所有 Gate 2/3 检查，调试/可信环境用

Mode 3: allowed-tools (白名单)
  → 只允许指定工具自动执行，其余仍需确认
  → 例: "bash:read, read_file, glob" = 读操作自动放行
```

**Slash 命令集成**:
```
/permissions ask              → 切换到 ask 模式
/permissions auto             → 切换到 auto-accept
/permissions allow bash:read  → bash 的读操作自动允许
/permissions status           → 查看当前模式和白名单
```

**关键概念**:
- Gate 1 硬拒绝**始终生效**，auto-accept 也不能绕过
- 白名单格式 `tool:pattern`，如 `bash:read` 表示包含 "read" 的 bash 命令自动放行
- 与 s03 的 PermissionPipeline 是**同一类的扩展**，替换而非新增

**新增文件**: `s22_permission_modes/code.py`

---

## 阶段八：工具生态扩展（s23-s25）

### s23 Code Search & Git — "在代码里找东西，管版本"

**目标**: 添加 grep 代码搜索 + git 操作工具集

**新增工具**:

| 工具 | 功能 | Claude Code 对应 |
|------|------|-----------------|
| `grep` | 正则搜索文件内容（ripgrep 驱动） | `Grep` |
| `git_status` | 显示工作区状态 | 内置 git 集成 |
| `git_diff` | 显示变更差异 | 同上 |
| `git_log` | 显示提交历史 | 同上 |
| `git_commit` | 提交变更 | 同上 |
| `git_branch` | 分支操作（创建/切换/列出） | 同上 |

**grep 工具接口**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `pattern` | string | ✅ | 正则表达式 |
| `path` | string | — | 搜索目录或文件，默认 "." |
| `glob` | string | — | 文件过滤，如 "*.py" |
| `context` | int | — | 匹配行前后显示的上下文行数 |

**git_diff 工具接口**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | — | 文件或目录 |
| `staged` | boolean | — | 是否显示暂存区变更 |

**关键概念**:
- `grep` 调用 ripgrep (`rg`)，需预装；找不到时 fallback 到 Python `re` 模块扫描
- git 工具走 `subprocess.run(["git", ...])`，不走 shell，避免注入风险
- `git_commit` 受权限管线控制（Gate 2: 写操作需确认）
- 所有输出截断到 50000 字符，与 s08 L3 budget 对齐

**新增文件**: `s23_code_search/code.py`

---

### s24 Web Tools — "连上互联网"

**目标**: Web 搜索 + 页面抓取，让 Agent 能查阅文档、搜索解决方案

**新增工具**:

| 工具 | 功能 | Claude Code 对应 |
|------|------|-----------------|
| `web_search` | 搜索引擎查询 | `WebSearch` |
| `web_fetch` | 抓取 URL 内容，转为文本/Markdown | `WebFetch` |

**架构**:

```
web_search("query")  → 搜索 API (Brave/SerpAPI) → 标题 + URL + 摘要
web_fetch(url)       → httpx.get → HTML → 文本/Markdown → 截断 50000 字符
```

**web_search 接口**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | ✅ | 搜索关键词 |
| `max_results` | int | — | 最大结果数，默认 5 |

**web_fetch 接口**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | ✅ | 目标 URL |
| `format` | string | — | "text"（默认）或 "markdown" |

**配置**: `SEARCH_API_KEY` 通过 `.env` 配置（Brave/SerpAPI key）

**关键概念**:
- `web_search` 需要搜索 API key，`web_fetch` 直接 HTTP GET 不需要额外 key
- HTML → Markdown 可用 `markdownify` 或 `html2text` 库
- `web_fetch` 应加入权限管线：检查 URL 是否在内网（防 SSRF）

**新增文件**: `s24_web_tools/code.py`

---

### s25 Multimedia & Notebook — "不只看文本"

**目标**: 多模态读取（图片/PDF）+ Jupyter Notebook 支持

**新增工具**:

| 工具 | 功能 | Claude Code 对应 |
|------|------|-----------------|
| `read_image` | 读取图片，返回 vision content block | `Read` (images) |
| `read_pdf` | 读取 PDF 指定页面，转为文本 | `Read` (PDFs) |
| `notebook_read` | 读取 .ipynb 为 cell 结构 | `Read` (notebooks) |
| `notebook_edit` | 编辑 notebook 中的 cell | `NotebookEdit` |

**read_image 接口**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | ✅ | 图片路径（png/jpg/gif/webp） |

返回 Anthropic vision 格式的 `image` content block（base64 编码），非纯文本。

**read_pdf 接口**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | ✅ | PDF 路径 |
| `pages` | string | — | 页码范围，如 "1-5", "3", "1-3,7" |

**notebook_edit 接口**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | ✅ | .ipynb 路径 |
| `cell_index` | int | ✅ | Cell 索引（0-based） |
| `source` | string | ✅ | 新的 cell 内容 |
| `cell_type` | string | — | "code" 或 "markdown" |

**关键概念**:
- 图片通过 Anthropic vision API 发送，需在 `agent/loop.py` 中支持 image content block
- PDF 用 `PyMuPDF` (`fitz`) 提取文本，需 `pip install pymupdf`
- Notebook 本质是 JSON，直接解析 `cells` 数组
- 图片大小限制 20MB，PDF 页数限制 20 页

**新增文件**: `s25_multimedia/code.py`

---

## 阶段九：会话与交互（s26-s28）

### s26 Session Management — "退出了还能回来"

**目标**: 会话持久化 + 恢复 + Token 成本追踪

**会话存储结构**:

```
.sessions/
  {session_id}.jsonl      ← 完整消息记录（每行一条 JSON message）
  {session_id}.meta.json  ← 元数据（创建时间、模型、token 用量）
  latest                   ← 文本文件，记录最近一次 session_id
```

**SessionManager 接口**:

| 方法 | 说明 |
|------|------|
| `create(session_id?)` | 创建新会话，返回 session_id |
| `save_turn(session_id, message)` | 追加一条消息到 JSONL |
| `load(session_id)` | 从 JSONL 恢复完整对话 |
| `list_sessions()` | 列出所有会话（按时间倒序） |
| `get_latest()` | 获取最近一次会话 ID |

**TokenTracker 接口**:

| 方法 | 说明 |
|------|------|
| `record(model, usage)` | 记录一次 LLM 调用的 token 用量 |
| `cost(model)` | 计算累计费用 (USD) |
| `summary(model)` | 返回可读的用量/费用摘要 |

**关键概念**:
- 会话用 JSONL 格式追加写入，不覆盖，不怕崩溃
- `latest` 文件记录最近会话 ID，实现 `/resume` 自动恢复
- TokenTracker 在每次 LLM 调用后记录，通过 `response.usage` 获取
- 费用计算基于官方定价表，可通过 `.env` 的 `CUSTOM_PRICING` 覆盖

**新增文件**: `s26_session/code.py`

---

### s27 Project Instructions — "项目有自己的规矩"

**目标**: 自动加载项目级指令文件，等同于 Claude Code 的 `CLAUDE.md`

**搜索顺序**（按优先级合并）:

```
1. .claude/CLAUDE.md        ← 项目级（最优先，提交到 git）
2. CLAUDE.md                ← 项目根目录（通用）
3. .claude/CLAUDE.local.md  ← 本地覆盖（不提交 git，个人配置）
4. ~/.claude/CLAUDE.md      ← 用户级（全局默认）
```

**文件格式**: 纯 Markdown，无需 frontmatter

**PromptAssembler 集成**:

注册为 `project_instructions` 段落，priority=5（identity 之后、behavior 之前），条件加载。

```
[priority 0]  identity:              "You are a coding agent at /workdir."
[priority 5]  project_instructions:  "不要修改 migrations/..."
[priority 10] behavior:              "Use tools to solve tasks..."
[priority 20] skills:                "Skills available: ..."
[priority 30] memory:                "Relevant memories..."
```

**关键概念**:
- 多个文件按优先级合并，都是追加不覆盖
- `.claude/CLAUDE.local.md` 适合个人配置（已在 .gitignore 中）
- 加载结果缓存，同一会话内只读一次磁盘

**新增文件**: `s27_project_instructions/code.py`

---

### s28 Slash Commands — "快捷操作入口"

**目标**: 内置斜杠命令，提供交互式控制和快捷操作

**命令清单**:

| 命令 | 功能 | Claude Code 对应 |
|------|------|-----------------|
| `/help` | 显示所有命令帮助 | `/help` |
| `/clear` | 清空对话历史，重新开始 | `/clear` |
| `/compact [strategy]` | 手动触发上下文压缩 | `/compact` |
| `/cost` | 显示 token 用量和费用 | `/cost` |
| `/sessions` | 列出历史会话 | — |
| `/resume [id]` | 恢复指定会话（无 id 则恢复最近） | — |
| `/permissions [mode]` | 查看/切换权限模式 | `/allowed-tools` |
| `/memory` | 显示当前记忆文件列表 | `/memory` |

**架构**:

```
用户输入
    ↓
SlashCommands.handle(text)
    ├─ 以 "/" 开头 → 匹配命令 → 执行 → 返回 True（不送 LLM）
    └─ 不以 "/" 开头 → 返回 False → 正常送 LLM
```

**与 main.py 的集成**:

```python
slash = SlashCommands(agent, session_mgr, token_tracker, permissions)
while True:
    query = input(">> ")
    if slash.handle(query):   # 斜杠命令已处理，跳过 LLM
        continue
    agent.run(history)
```

**关键概念**:
- 斜杠命令在 CLI 入口拦截，不送入 LLM，不消耗 token
- `/clear` 清空内存中的 messages，但不影响磁盘上的会话文件
- `/compact` 调用 s08 的压缩管线，等同于自动压缩但由用户手动触发
- `/resume` 从 `.sessions/` 加载 JSONL，替换当前 messages
- `/permissions` 支持三种模式切换，与 s22 的 PermissionPipeline 联动
- 命令匹配用精确前缀，不支持模糊匹配（避免歧义）

**新增文件**: `s28_slash_commands/code.py`

---

## 推荐学习节奏

```
Week 1:  s01 → s02 → s03 → s04    基础循环 + 工具 + 权限 + Hooks
         重点: 理解 agent loop 核心模式
         产出: 能跑的最小 agent

Week 2:  s05 → s06 → s07 → s08    计划 + 子Agent + 技能 + 压缩
         重点: 理解上下文管理
         产出: 能做复杂多步任务的 agent

Week 3:  s09 → s10 → s11          记忆 + Prompt组装 + 错误恢复
         重点: 理解持久化和容错
         产出: 能记住偏好的健壮 agent

Week 4:  s12 → s13 → s14          任务系统 + 后台 + 定时
         重点: 理解长期运行
         产出: 能持久化任务的 agent

Week 5:  s15 → s16 → s17 → s18   团队 + 协议 + 自治 + 隔离
         重点: 理解多 agent 协作
         产出: 能组队工作的 agent 系统

Week 6:  s19 → s20                MCP + 全机制整合
         重点: 理解外部能力集成
         产出: 完整的 agent harness

Week 7:  s21 → s22                沙箱 + 权限模式
         重点: 理解安全执行和权限分级
         产出: 能安全执行命令的 agent

Week 8:  s23 → s24 → s25          代码搜索 + Web + 多模态
         重点: 理解工具生态扩展
         产出: 能搜索代码、查阅文档、读图/PDF 的 agent

Week 9:  s26 → s27 → s28          会话管理 + 项目指令 + 斜杠命令
         重点: 理解持久化交互和用户体验
         产出: 接近 Claude Code 生产级体验的 agent
```

## 每章学习步骤

```
1. 阅读 README.md (中文源文档，完整叙事)
2. 阅读 code.py (可运行代码)
3. 运行 code.py，体验交互
4. 对比上一章的 code.py，找出差异
5. 在自己的项目中实现该模块
6. 测试 → 下一章
```

## 项目结构建议

```
your-agent/
  .env                      # API Key 配置
  .claude/
    CLAUDE.md               # s27: 项目级指令
    CLAUDE.local.md         # s27: 本地覆盖（不提交 git）
  .memory/                  # s09: 持久记忆
  .tasks/                   # s12: 持久化任务
  .transcripts/             # s08: 压缩前的对话记录
  .task_outputs/            # s08: 大输出持久化
  .sessions/                # s26: 会话持久化
  .mailboxes/               # s15: 队友邮箱
  .worktrees/               # s18: 隔离工作区
  skills/                   # s07: 技能定义
    agent-builder/SKILL.md
    code-review/SKILL.md
  s01_agent_loop/
    code.py
    README.md
  s02_tool_use/
    code.py
    README.md
  ...
  s20_comprehensive/
    code.py
    README.md
  s21_sandbox/
    code.py
    Dockerfile
    README.md
  s22_permission_modes/
    code.py
    README.md
  s23_code_search/
    code.py
    README.md
  s24_web_tools/
    code.py
    README.md
  s25_multimedia/
    code.py
    README.md
  s26_session/
    code.py
    README.md
  s27_project_instructions/
    code.py
    README.md
  s28_slash_commands/
    code.py
    README.md
```

## 关键设计原则

1. **循环永远不变** — 从 s01 到 s20，`while stop_reason == "tool_use"` 的核心模式始终一致
2. **增量式构建** — 每章只新增一个机制，前一章代码完整保留
3. **便宜先跑** — 压缩管线中 L1→L2→L3 零 API 调用，L4 才调 LLM
4. **信任模型** — 不硬编码工作流，给工具让模型自己推理
5. **上下文隔离** — 子 Agent 用全新 `messages[]`，只返回摘要
6. **挂载不侵入** — 扩展通过 hook 注册，不改循环体

## 参考资源

- 仓库: https://github.com/shareAI-lab/learn-claude-code
- 文档语言: 中文 (docs/zh/)、英文 (docs/en/)、日文 (docs/ja/)
- 姊妹项目: https://github.com/shareAI-lab/claw0 (主动式常驻助手)
- Kode CLI: https://github.com/shareAI-lab/Kode-cli
- Kode SDK: https://github.com/shareAI-lab/Kode-agent-sdk
