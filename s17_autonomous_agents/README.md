# s17 Autonomous Agents — "自己看板认领活"

## 目标

让队友 Agent 能够自主认领任务，无需 Lead 逐个分配。

## 核心概念

### 问题

s15/s16 的队友只能被动等待 Lead 分配任务：
1. Lead 需要手动调用 `spawn_teammate` 为每个任务派生队友
2. 队友完成任务后进入 IDLE，只能等待新消息，不能主动找工作
3. 如果有多个待执行任务，Lead 需要逐个分配，效率低

### 方案

在 IDLE 阶段增加自动认领逻辑：
1. 队友每 5 秒扫描一次任务看板
2. 查找 `status=pending, owner=None, 依赖已完成` 的任务
3. 自动认领并进入 WORK 阶段执行
4. 如果 60 秒没有找到可认领的任务，自动关机

## 生命周期

```
WORK: inbox → LLM → tools → (tool_use? loop) → (done? → IDLE)
IDLE: 5s poll → inbox? → WORK / unclaimed? → claim → WORK / 60s? → SHUTDOWN
```

### 状态转换图

```
                    ┌─────────────────────────────────────┐
                    │                                     │
                    ▼                                     │
    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌─────┴────┐
    │  START   │───▶│   WORK   │───▶│   IDLE   │───▶│ SHUTDOWN │
    └──────────┘    └──────────┘    └──────────┘    └──────────┘
                         │               │
                         │               │ 收到消息
                         │               ▼
                         │          ┌──────────┐
                         └──────────│ WORK_MSG │
                                    └──────────┘
                         │               │
                         │               │ 认领任务
                         │               ▼
                         │          ┌──────────────┐
                         └──────────│ WORK_CLAIMED │
                                    └──────────────┘
```

## 实现细节

### 自动认领逻辑

```python
def scan_unclaimed_tasks(store: TaskStore) -> list:
    """扫描未认领的、依赖已完成的 pending 任务"""
    tasks = store.list_all()
    return [
        t for t in tasks
        if t.status == "pending"
        and t.owner is None
        and can_start(t, store)
    ]
```

### IDLE 循环增强

```python
# IDLE 阶段循环
while True:
    # 1. 检查邮箱是否有新消息
    inbox = self.bus.receive(name)
    if inbox:
        # 处理消息，进入 WORK 状态
        ...

    # 2. s17: 自动扫描任务看板
    if self.task_store:
        unclaimed = scan_unclaimed_tasks(self.task_store)
        if unclaimed:
            # 认领第一个可用任务
            task = unclaimed[0]
            task.status = "in_progress"
            task.owner = name
            self.task_store.save(task)

            # 执行认领的任务
            ...

    # 3. 空闲超时检查
    if time.time() - idle_start > idle_timeout:
        # 自动关机
        ...

    time.sleep(5)  # 每 5 秒轮询一次
```

## 新增工具

队友现在拥有以下任务管理工具：

| 工具 | 功能 | 说明 |
|------|------|------|
| `list_tasks` | 查看任务列表 | 可按 status 过滤 |
| `claim_task` | 认领任务 | 设置 owner 和 status=in_progress |
| `complete_task` | 完成任务 | 设置 status=completed |

## 修改的文件

### tools/teams.py

1. **TeamManager.__init__**: 新增 `task_store` 参数
2. **_build_teammate_registry**: 给队友注册 `list_tasks`, `claim_task`, `complete_task` 工具
3. **spawn 方法 IDLE 阶段**: 集成自动认领逻辑

### main.py

1. 创建 `TeamManager` 时传入 `task_store`
2. 更新 behavior prompt，说明队友会自动认领任务

## 使用示例

### Lead Agent 视角

```
>> 创建两个任务
create_task(subject="实现登录功能", description="实现用户登录 API")
create_task(subject="实现注册功能", description="实现用户注册 API")

>> 派生队友（不需要指定具体任务）
spawn_teammate(task="You are a teammate agent. Work on tasks from the task board.")

>> 队友会自动认领任务并执行
team_status()
```

### Teammate Agent 行为

1. 启动后执行初始任务（如果有）
2. 进入 IDLE 状态
3. 每 5 秒扫描任务看板
4. 发现可认领任务 → 自动认领并执行
5. 完成任务后继续扫描
6. 60 秒无任务 → 自动关机

## 关键设计

### 为什么是 5 秒轮询？

- 太频繁（1秒）：浪费 CPU，增加文件系统压力
- 太慢（30秒）：响应延迟高，用户体验差
- 5 秒是平衡点：响应及时，资源消耗合理

### 为什么是 60 秒超时？

- 太短（10秒）：可能错过刚创建的任务
- 太长（300秒）：浪费资源，队友空转
- 60 秒是平衡点：给足时间等待新任务，不会无限等待

### 为什么认领第一个可用任务？

- 简单可靠，不需要复杂的任务分配算法
- 多个队友时，先到先得
- 未来可以扩展优先级、负载均衡等策略

## 与 s15/s16 的关系

- **s15**: 提供基础的消息总线和队友管理
- **s16**: 提供协议机制（shutdown, plan approval）
- **s17**: 在 IDLE 阶段增加自动认领，让队友更自主

## 未来扩展

1. **任务优先级**: 认领高优先级任务
2. **负载均衡**: 根据队友当前负载分配任务
3. **技能匹配**: 根据队友技能匹配任务
4. **协作任务**: 多个队友协作完成一个任务
