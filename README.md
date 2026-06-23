# Claude Agent 实现项目

从零学习并实现一个完整的 Claude Agent，基于 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 课程。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
cp .env.example .env
# 编辑 .env 填入你的 ANTHROPIC_API_KEY

# 3. 运行
python main.py
```

## 当前项目结构

```
claude_agent_impl/
├── main.py                 # 入口：组装模块 + CLI
├── .env                    # API 配置（不入 git）
├── .env.example            # 配置模板
├── .gitignore
├── requirements.txt        # Python 依赖
├── README.md               # 本文件
├── IMPLEMENTATION_PLAN.md  # 完整实现方案（参考用）
│
├── .claude/                # Claude Code 配置目录
│   ├── skills/             # s07: 技能定义（SKILL.md）
│   │   ├── code-review/
│   │   └── agent-builder/
│   ├── rules/              # 规则（未来）
│   └── commands/           # 自定义命令（未来）
│
├── agent/                  # 核心
│   ├── __init__.py
│   ├── config.py           # 配置加载
│   ├── loop.py             # 核心 while 循环
│   ├── runtime.py          # s23: 单 turn 生命周期管线
│   ├── state.py            # s23: LoopState 运行状态
│   ├── interceptor.py      # s23: 模型/工具拦截链
│   ├── events.py           # s24: 结构化 Runtime Event
│   ├── event_store.py      # s25: 本地事件与 run 持久化
│   └── event_bus.py        # s26: 本进程事件分发与观测订阅者
│
├── tools/                  # 工具层
│   ├── __init__.py
│   ├── registry.py         # 工具注册表 + 分发
│   ├── bash.py             # bash 工具
│   ├── file_ops.py         # read/write/edit/glob
│   ├── todo.py             # s05: 计划工具
│   └── subagent.py         # s06: 子 agent
│
├── skills/                 # s07: 技能加载器（Python 模块）
│   ├── __init__.py
│   └── loader.py
│
├── permissions/            # 权限管控
│   ├── __init__.py
│   └── pipeline.py         # s03: 三级权限管线
│
└── hooks/                  # 扩展点
    ├── __init__.py
    └── manager.py          # s04: Hook 管理器
```

## 目标项目结构（全部模块实现后）

```
claude_agent_impl/
├── main.py                     # 入口：组装模块 + CLI
├── .env
├── .gitignore
├── requirements.txt
├── README.md
├── IMPLEMENTATION_PLAN.md
│
├── agent/                      # 核心
│   ├── __init__.py
│   ├── config.py               # 配置加载
│   ├── loop.py                 # 核心 while 循环
│   ├── runtime.py              # 企业级生命周期管线
│   ├── state.py                # turn/run 状态与关联 ID
│   ├── interceptor.py          # 横切拦截链
│   ├── events.py               # 结构化事件流
│   ├── event_store.py          # Runtime Event 仓储
│   ├── event_bus.py            # EventPublisher / EventBus / Metrics / Trace
│   └── subagent.py             # s06: 子 Agent 派生
│
├── tools/                      # 工具层
│   ├── __init__.py
│   ├── registry.py             # 工具注册表 + 分发
│   ├── bash.py                 # bash 工具
│   ├── file_ops.py             # read/write/edit/glob
│   └── todo.py                 # s05: 计划工具
│
├── hooks/                      # 扩展点
│   ├── __init__.py
│   └── manager.py              # s04: Hook 管理器
│
├── permissions/                # 权限管控
│   ├── __init__.py
│   └── pipeline.py             # s03: 三级权限管线
│
├── memory/                     # 跨会话记忆
│   ├── __init__.py
│   └── manager.py              # s09: 记忆管理器
│
├── compaction/                 # 上下文压缩
│   ├── __init__.py
│   └── pipeline.py             # s08: 四层压缩管线
│
├── prompt/                     # Prompt 组装
│   ├── __init__.py
│   └── assembler.py            # s10: 动态 Prompt 组装
│
├── recovery/                   # 错误恢复
│   ├── __init__.py
│   └── handler.py              # s11: 三条恢复路径
│
├── skills/                     # 技能加载
│   ├── __init__.py
│   ├── loader.py               # s07: 两级按需加载
│   └── definitions/            # 技能定义文件
│       ├── agent-builder/SKILL.md
│       └── code-review/SKILL.md
│
├── tasks/                      # 任务系统
│   ├── __init__.py
│   └── manager.py              # s12: 持久化任务图
│
├── background/                 # 后台任务
│   ├── __init__.py
│   └── executor.py             # s13: 线程异步执行
│
├── scheduler/                  # 定时调度
│   ├── __init__.py
│   └── cron.py                 # s14: Cron 调度器
│
├── teams/                      # 多 Agent 协作
│   ├── __init__.py
│   ├── bus.py                  # s15: 消息总线
│   ├── protocols.py            # s16: 团队协议
│   └── autonomous.py           # s17: 自治 Agent (已合并入 tools/task.py)
│
├── worktree/                   # 工作区隔离 (已合并入 tools/worktree.py)
│   ├── __init__.py
│   └── manager.py              # s18: Git Worktree 管理 (已合并)
│
├── mcp/                        # MCP 集成
│   ├── __init__.py
│   └── client.py               # s19: MCP 客户端
│
└── data/                       # 运行时数据（不入 git）
    ├── memory/                 # s09: 持久记忆文件
    ├── tasks/                  # s12: 持久化任务
    ├── mailboxes/              # s15: 队友邮箱
    ├── worktrees/              # s18: 隔离工作区
    ├── transcripts/            # s08: 压缩前对话记录
    ├── tool-results/           # s08: 大输出持久化
    └── runtime/                # s25: Runtime 事件和 run 记录
        ├── events/runtime-events.jsonl
        └── runs/{run_id}.json
```

## 实现进度

| 模块 | 文件 | 状态 | 说明 |
|------|------|------|------|
| s01 Agent Loop | agent/loop.py | ✅ | 核心 while 循环 |
| s02 Tool Use | tools/*.py | ✅ | 5 个工具 + 分发映射 |
| s03 Permission | permissions/pipeline.py | ⬜ | 三级权限管线 |
| s04 Hooks | hooks/manager.py | ⬜ | Hook 扩展点 |
| s05 TodoWrite | tools/todo.py | ⬜ | 计划工具 |
| s06 Subagent | agent/subagent.py | ⬜ | 子 Agent |
| s07 Skill Loading | skills/loader.py | ✅ | 两级按需加载 |
| s08 Context Compact | compaction/pipeline.py | ⬜ | 上下文压缩 |
| s09 Memory | memory/manager.py | ⬜ | 跨会话记忆 |
| s10 System Prompt | prompt/assembler.py | ✅ | 动态 Prompt 组装 |
| s11 Error Recovery | recovery/handler.py | ⬜ | 错误恢复 |
| s12 Task System | tasks/manager.py | ⬜ | 持久化任务图 |
| s13 Background Tasks | background/executor.py | ⬜ | 后台执行 |
| s14 Cron Scheduler | scheduler/cron.py | ⬜ | 定时调度 |
| s15 Agent Teams | teams/bus.py | ⬜ | 多 Agent 协作 |
| s16 Team Protocols | teams/protocols.py | ⬜ | 团队协议 |
| s17 Autonomous Agents | tools/task.py | ✅ | 自治 Agent (已合并) |
| s18 Worktree Isolation | tools/worktree.py | ✅ | 工作区隔离 |
| s19 MCP Plugin | mcp/client.py | ⬜ | MCP 工具集成 |
| s20 Comprehensive | main.py | ⬜ | 全机制整合 |
| s21 Command Sandbox | tools/sandbox.py | ✅ | 跨平台命令沙箱 |
| s22 Enterprise Baseline | tools/result.py, tests/, docs/threat-model.md | ✅ | 安全回归、最小 ToolResult、威胁模型 |
| s23 Runtime Skeleton | agent/state.py, agent/interceptor.py, agent/runtime.py | ✅ | LoopState、Pipeline、Interceptor |
| s24 Structured Events | agent/events.py, agent/state.py, agent/loop.py | ✅ | RuntimeEvent、事件流、artifact_refs |
| s25 Persistence | agent/event_store.py, agent/runtime.py | ✅ | JSONL 事件仓储、run 摘要持久化 |
| s26 Observability | agent/event_bus.py, agent/state.py, agent/runtime.py | ✅ | EventPublisher、InProcessEventBus、Metrics、Trace |

## s22 Enterprise Baseline

```powershell
# 使用默认 tacn 环境运行 s22 安全与运行时基线测试
D:\Language\anaconda3\envs\tacn\python.exe -m pytest tests/security tests/runtime
```

s22 保持现有 agent loop 的字符串返回兼容，同时新增最小 `ToolResult` 结构，供测试、审计和后续 Runtime Event 使用。本阶段锁住前台 bash、后台 bash、环境变量清理、危险命令拦截、输出截断、未知工具和 handler 异常这些 P0 风险。

CI 门禁位于 `.github/workflows/s22-baseline.yml`，push / pull_request 时自动运行同一组基线测试。

## s23 Runtime Skeleton

```powershell
# 运行 Runtime 骨架、拦截器和 s22 基线测试
D:\Language\anaconda3\envs\tacn\python.exe -m pytest tests/runtime tests/security
```

s23 新增 `LoopState`、`AgentRuntime` 和 `InterceptorChain`。当前 CLI 已通过 `AgentRuntime.execute(state)` 调用原有 `AgentLoop`，模型调用和工具调用都经过默认空拦截链；默认行为不变，但后续 Trace、策略、限流、审计已经有统一挂载点。

阅读这部分代码时可以按这个顺序看：

1. `main.py`：组装工具、hooks、prompt、runtime，并为整个 CLI 会话生成一个稳定的 `session_id`。
2. `agent/runtime.py`：定义 `preflight -> build_context -> react_loop -> finalize` 生命周期。
3. `agent/state.py`：记录一次 turn 的 ID、状态、phase 耗时、模型调用和工具调用事实。
4. `agent/interceptor.py`：提供模型/工具调用的横切扩展点。
5. `agent/loop.py`：保留原有 ReAct 主循环，只在模型和工具调用处接入 state 与 interceptor。

## s24 Structured Events

```powershell
# 运行结构化事件、Runtime 和安全基线测试
D:\Language\anaconda3\envs\tacn\python.exe -m pytest tests/runtime tests/security
```

s24 新增 `RuntimeEvent`，并让 `AgentRuntime` 和 `AgentLoop` 在关键节点写入 `state.events`。事件覆盖 run 生命周期、phase 切换、模型调用、工具调用结果；工具层继续兼容字符串展示，同时通过 `ToolResult.to_dict()` 给事件流提供稳定结构。大输出和文件产物通过 `artifact_refs` 预留引用，不要求把完整内容塞回 prompt。

阅读这部分代码时可以按这个顺序看：

1. `tools/result.py`：工具执行事实的最小结构，区分 `stdout`、`stderr`、`ok`、`error_code` 和 `artifact_refs`。
2. `agent/events.py`：Runtime Event 的统一 schema，负责带上 session、turn、run、trace、request 等关联 ID。
3. `agent/state.py`：通过 `emit_event()` 把事件按 sequence 追加到 `state.events`。
4. `agent/runtime.py`：发出 `run_started/run_finished/run_failed` 和 `phase_started/phase_finished`。
5. `agent/loop.py`：发出 `model_started/model_finished` 与 `tool_started/tool_finished`。

## s25 Persistence

```powershell
# 运行持久化、Runtime 和安全基线测试
D:\Language\anaconda3\envs\tacn\python.exe -m pytest tests/runtime tests/security
```

s25 新增 `JsonlEventStore`，把 s24 的内存事件流落到 `data/runtime/events/runtime-events.jsonl`，并把每次 run 的摘要写入 `data/runtime/runs/{run_id}.json`。当前实现仍然是本地文件仓储，便于学习和测试；接口通过 `RuntimeEventStore` 收窄，后续可以替换为 SQLite、PostgreSQL 或 outbox 实现。

阅读这部分代码时可以按这个顺序看：

1. `agent/event_store.py`：定义 `RuntimeEventStore` 协议和 `JsonlEventStore` 文件仓储。
2. `agent/runtime.py`：在成功和失败路径统一调用 `_persist(state)`。
3. `main.py`：CLI 默认使用 `JsonlEventStore.for_workdir(config.workdir)`。
4. `tests/runtime/test_event_store.py`：验证事件写入、过滤、去重、成功 run 和失败 run 持久化。

## s26 Observability

```powershell
# 运行事件分发、观测订阅者、Runtime 和安全基线测试
D:\Language\anaconda3\envs\tacn\python.exe -m pytest tests/runtime tests/security
```

s26 新增本进程事件分发层。`LoopState.emit_event()` 仍负责生成事件和追加到 `state.events`，但如果 state 上挂了 `EventPublisher`，事件会同步发布到 `InProcessEventBus`。订阅者可以按事件类型或 `*` 通配符消费事件，当前内置 `RuntimeMetrics` 和 `RuntimeTraceRecorder` 两个观测订阅者。CLI 默认创建空 bus 和 publisher，后续审计、SSE、告警、外部消息队列都可以挂到这条分发路径上。

阅读这部分代码时可以按这个顺序看：

1. `agent/event_bus.py`：定义 `EventPublisher`、`InProcessEventBus`、`RuntimeMetrics` 和 `RuntimeTraceRecorder`。
2. `agent/state.py`：`emit_event()` 在 append 后调用 publisher 分发事件。
3. `agent/runtime.py`：执行前把 `event_publisher` 挂到当前 `LoopState`。
4. `main.py`：CLI 默认创建 `InProcessEventBus` 和 `EventPublisher`。
5. `tests/runtime/test_event_bus.py`：验证订阅、通配符、消费者异常隔离、metrics、trace 和 Runtime 集成。

## 学习路径

```
阶段一: s01 Agent Loop → s02 Tool Use → s03 Permission → s04 Hooks
阶段二: s05 TodoWrite → s06 Subagent → s07 Skill Loading → s08 Context Compact
阶段三: s09 Memory → s10 System Prompt → s11 Error Recovery
阶段四: s12 Task System → s13 Background Tasks → s14 Cron Scheduler
阶段五: s15 Agent Teams → s16 Team Protocols → s17 Autonomous Agents → s18 Worktree Isolation
阶段六: s19 MCP Plugin → s20 Comprehensive Agent
企业级升级: s21 Command Sandbox → s22 Enterprise Baseline → s23 Runtime Skeleton → s24 Structured Events → s25 Persistence → s26 Observability
```

## 运行环境

- Python: 3.12 (tacn)
- 依赖: anthropic, python-dotenv, pyyaml
- 模型: mimo-v2.5-pro (MiMo 兼容提供商)
