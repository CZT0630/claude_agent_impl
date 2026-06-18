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
│   └── loop.py             # 核心 while 循环
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
    └── tool-results/           # s08: 大输出持久化
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

## 学习路径

```
阶段一: s01 Agent Loop → s02 Tool Use → s03 Permission → s04 Hooks
阶段二: s05 TodoWrite → s06 Subagent → s07 Skill Loading → s08 Context Compact
阶段三: s09 Memory → s10 System Prompt → s11 Error Recovery
阶段四: s12 Task System → s13 Background Tasks → s14 Cron Scheduler
阶段五: s15 Agent Teams → s16 Team Protocols → s17 Autonomous Agents → s18 Worktree Isolation
阶段六: s19 MCP Plugin → s20 Comprehensive Agent
```

## 运行环境

- Python: 3.12 (tacn)
- 依赖: anthropic, python-dotenv, pyyaml
- 模型: mimo-v2.5-pro (MiMo 兼容提供商)
