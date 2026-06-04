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
├── main.py                 # 入口：配置 + 工具 + 核心循环 + CLI
├── .env                    # API 配置（不入 git）
├── .env.example            # 配置模板
├── requirements.txt        # Python 依赖
├── README.md               # 本文件
└── IMPLEMENTATION_PLAN.md  # 完整实现方案（参考用）
```

## 实现进度

| 模块 | 文件 | 状态 | 说明 |
|------|------|------|------|
| s01 Agent Loop | main.py | ✅ | 核心 while 循环 |
| s02 Tool Use | main.py | ✅ | 5 个工具 + 分发映射 |
| s03 Permission | - | ⬜ | 三级权限管线 |
| s04 Hooks | - | ⬜ | Hook 扩展点 |
| s05 TodoWrite | - | ⬜ | 计划工具 |
| s06 Subagent | - | ⬜ | 子 Agent |
| s07 Skill Loading | - | ⬜ | 技能按需加载 |
| s08 Context Compact | - | ⬜ | 上下文压缩 |
| s09 Memory | - | ⬜ | 跨会话记忆 |
| s10 System Prompt | - | ⬜ | 动态 Prompt 组装 |
| s11 Error Recovery | - | ⬜ | 错误恢复 |
| s12 Task System | - | ⬜ | 持久化任务图 |
| s13 Background Tasks | - | ⬜ | 后台执行 |
| s14 Cron Scheduler | - | ⬜ | 定时调度 |
| s15 Agent Teams | - | ⬜ | 多 Agent 协作 |
| s16 Team Protocols | - | ⬜ | 团队协议 |
| s17 Autonomous Agents | - | ⬜ | 自治 Agent |
| s18 Worktree Isolation | - | ⬜ | 工作区隔离 |
| s19 MCP Plugin | - | ⬜ | MCP 工具集成 |
| s20 Comprehensive | - | ⬜ | 全机制整合 |

## 目标项目结构

当模块增多后，拆分为模块化结构：

```
claude_agent_impl/
├── main.py                 # 入口：组装模块 + CLI
├── .env
├── requirements.txt
├── README.md
│
├── agent/
│   ├── loop.py             # 核心循环
│   └── config.py           # 配置加载
│
├── tools/
│   ├── registry.py         # 工具注册表 + 分发
│   ├── bash.py             # bash 工具
│   └── file_ops.py         # read/write/edit/glob
│
├── hooks/
│   └── manager.py          # Hook 管理器
│
├── permissions/
│   └── pipeline.py         # 权限管线
│
├── memory/
│   └── manager.py          # 跨会话记忆
│
├── compaction/
│   └── pipeline.py         # 上下文压缩
│
├── skills/
│   └── loader.py           # 技能加载
│
└── tasks/
    └── manager.py          # 任务系统
```

## 学习路径

```
阶段一: s01 Agent Loop → s02 Tool Use → s03 Permission → s04 Hooks
阶段二: s05 TodoWrite → s06 Subagent → s07 Skill Loading → s08 Context Compact
阶段三: s09 Memory → s10 System Prompt → s11 Error Recovery
阶段四: s12 Task System → s13 Background Tasks → s14 Cron Scheduler
阶段五: s15 Agent Teams → s16 Team Protocols → s17 Autonomous Agents → s18 Worktree Isolation
阶段六: s19 MCP Plugin → s20 Comprehensive Agent
```

## 文档

- [完整实现方案](IMPLEMENTATION_PLAN.md) — 20 个模块的详细设计与代码

## 运行环境

- Python: 3.12 (tacn)
- 依赖: anthropic, python-dotenv
- 模型: mimo-v2.5-pro (MiMo 兼容提供商)
