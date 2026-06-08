"""
错误恢复模块 — 三条恢复路径 + 指数退避

Path 1: max_tokens   → 升级 8K→64K + continuation prompt（最多 3 次）
Path 2: prompt_too_long → reactive compact → 重试（1 次）
Path 3: 429/529      → 指数退避 + jitter（最多 10 次）→ fallback 模型
"""

import random
import time
from dataclasses import dataclass, field

# ── 常量 ──────────────────────────────────────────────────────────────

ESCALATED_MAX_TOKENS = 64_000       # max_tokens 升级上限
MAX_CONTINUATION = 3                # continuation prompt 最大次数
MAX_RETRIES = 10                    # 速率限制最大重试次数
BASE_DELAY_MS = 1000                # 退避基础延迟 (毫秒)
MAX_529_BEFORE_FALLBACK = 5         # 连续 529 多少次后切 fallback

CONTINUATION_PROMPT = (
    "Continue your response exactly where you left off. "
    "Do not repeat anything you already said."
)


# ── 恢复状态 ──────────────────────────────────────────────────────────

@dataclass
class RecoveryState:
    """跟踪整次对话中的恢复状态，挂在 AgentLoop 上。"""
    escalated: bool = False
    # Path 1 用到的字段:
    #   escalated — max_tokens 是否已从 8K 升级到 64K（整个对话只升级一次）
    #   continuation_count — 已向 LLM 发送了几次 "Continue..." prompt（上限 MAX_CONTINUATION=3）
    continuation_count: int = 0
    # Path 3 用到的字段:
    #   retry_count — 当前退避重试第几轮（上限 MAX_RETRIES=10），成功后清零
    #   consecutive_529 — 连续收到 529 的次数，成功后清零；达到 MAX_529_BEFORE_FALLBACK=5 时切备用模型
    consecutive_529: int = 0
    retry_count: int = 0

    def on_success(self):
        """调用成功时重置 529 计数，其余状态保留。"""
        self.consecutive_529 = 0
        self.retry_count = 0

    def reset(self):
        """完全重置（新一轮用户提问时可选调用）。"""
        self.escalated = False
        self.continuation_count = 0
        self.consecutive_529 = 0
        self.retry_count = 0


# ── Path 1: max_tokens 截断 ──────────────────────────────────────────

def handle_max_tokens(
    state: RecoveryState,
    messages: list,
    current_max_tokens: int,
) -> tuple[int, bool]:
    """
    Path 1: stop_reason == "max_tokens" 时调用。

    恢复策略分两步:
      1. 第一次截断 → 把 max_tokens 从 8K 升级到 64K（escalated=True，整个对话只升一次）
      2. 后续截断   → 往 messages 里追加一条 "Continue..." user 消息，
                      让 LLM 接着上次截断的地方继续输出（最多追加 3 次）

    Args:
        state:              恢复状态机（跨调用持久）
        messages:           对话历史（引用传递，append 会直接修改原列表）
        current_max_tokens: 当前的 max_tokens 设置

    Returns:
        (new_max_tokens, should_retry)
        - new_max_tokens: 可能升级到 ESCALATED_MAX_TOKENS (64K)
        - should_retry:   True=重新调用 LLM, False=放弃恢复返回当前 response
    """
    # 第一次截断: 升级 token 上限，不需要改 messages
    if not state.escalated:
        state.escalated = True
        return ESCALATED_MAX_TOKENS, True

    # 后续截断: 注入 "Continue..." prompt，让 LLM 接着说
    # continuation_count < MAX_CONTINUATION (3) 表示还没用完配额
    if state.continuation_count < MAX_CONTINUATION:
        messages.append({"role": "user", "content": CONTINUATION_PROMPT})
        state.continuation_count += 1
        return ESCALATED_MAX_TOKENS, True

    # 3 次 "Continue..." 都用完了还截断 → 放弃，返回当前（截断的）response
    return current_max_tokens, False


# ── Path 2: prompt_too_long ─────────────────────────────────────────

def handle_prompt_too_long(
    state: RecoveryState,
    compact_pipeline,
    messages: list,
) -> bool:
    """
    Path 2: API 返回 prompt_too_long 时调用。

    恢复策略: 用 s08 的 reactive_compact 紧急压缩 messages（snip + micro + LLM 摘要），
    压缩后重试一次。如果压缩完还太长就没救了，返回 False 让调用方 raise。

    Args:
        state:           恢复状态机（本路径暂未用到，预留）
        compact_pipeline: s08 的 CompactPipeline 实例，提供 reactive_compact 方法
        messages:        对话历史（引用传递，reactive_compact 会原地修改）

    Returns:
        should_retry — True=压缩完毕可以重试, False=没有压缩能力无法恢复
    """
    if not compact_pipeline:
        return False

    # reactive_compact 内部: budget → snip(更激进,只留20条) → micro → LLM 摘要
    compact_pipeline.reactive_compact(messages)
    return True


# ── Path 3: 速率限制 (429/529) ───────────────────────────────────────

def backoff_delay(attempt: int) -> float:
    """
    指数退避 + jitter，返回需要等待的秒数。

    每次重试等待时间翻倍，加随机抖动避免"惊群效应"（所有客户端同时重试）。

    Args:
        attempt: 当前是第几次重试（从 0 开始）

    Returns:
        等待秒数，例如:
          attempt=0 → 1.0~1.5s
          attempt=1 → 2.0~3.0s
          attempt=2 → 4.0~6.0s
          attempt=9 → 512~768s (~8-12分钟)
    """
    delay_ms = BASE_DELAY_MS * (2 ** attempt)       # 指数增长: 1s, 2s, 4s, 8s...
    jitter = random.uniform(0, delay_ms * 0.5)       # 0~50% 的随机抖动
    return (delay_ms + jitter) / 1000


def handle_rate_limit(
    state: RecoveryState,
    is_529: bool = False,
) -> bool:
    """
    Path 3: 429（限流）或 529（过载）时调用。

    恢复策略: 指数退避等待后重试，最多 MAX_RETRIES (10) 次。
    529 比 429 更严重，额外累计 consecutive_529 计数——
    当 consecutive_529 >= MAX_529_BEFORE_FALLBACK (5) 时，
    调用方应切换到 fallback 模型。

    Args:
        state:  恢复状态机，retry_count 和 consecutive_529 会被修改
        is_529: 是否是 529 过载错误（429 限流只增 retry_count，529 额外增 consecutive_529）

    Returns:
        should_retry — True=等待完毕可以重试, False=重试次数用尽放弃
    """
    if state.retry_count >= MAX_RETRIES:
        return False

    delay = backoff_delay(state.retry_count)
    state.retry_count += 1

    if is_529:
        state.consecutive_529 += 1      # 529 单独计数，用于判断是否切 fallback

    time.sleep(delay)                   # 阻塞等待，给服务器喘息时间
    return True


def should_switch_fallback(state: RecoveryState) -> bool:
    """
    判断是否应切换到 fallback 模型。

    连续 529 达到 MAX_529_BEFORE_FALLBACK (5) 次时返回 True，
    说明主模型持续过载，切到备用模型继续服务。

    Args:
        state: 恢复状态机，读取 consecutive_529 计数

    Returns:
        True=应切换, False=继续用主模型
    """
    return state.consecutive_529 >= MAX_529_BEFORE_FALLBACK


# ── 错误分类 ─────────────────────────────────────────────────────────

def classify_error(error: Exception) -> str:
    """
    将异常归类到三条恢复路径之一。

    用字符串匹配而非异常类型匹配，因为兼容提供商（DeepSeek、MiMo 等）
    可能抛不同的异常类，但错误消息格式相似。

    Args:
        error: LLM 调用抛出的异常

    Returns:
        路径标识:
          "prompt_too_long"  → Path 2, 上下文太长
          "rate_limit_529"   → Path 3, 服务过载（比 429 更严重）
          "rate_limit_429"   → Path 3, 请求限流
          "unknown"          → 不可恢复，直接 raise
    """
    msg = str(error).lower()

    # Path 2: 上下文超过模型的 context window
    if "prompt_too_long" in msg or "context_length_exceeded" in msg:
        return "prompt_too_long"

    # Path 3: 529 overloaded — 服务端过载，比 429 更严重
    if "529" in msg or "overloaded" in msg:
        return "rate_limit_529"

    # Path 3: 429 rate limit — 请求频率超限
    if "429" in msg or ("rate" in msg and "limit" in msg):
        return "rate_limit_429"

    return "unknown"
