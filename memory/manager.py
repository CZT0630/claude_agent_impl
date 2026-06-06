"""
记忆管理器 — 跨会话持久记忆

三个子系统:
    Selection:     LLM 调用前，按相关性选择记忆注入上下文
    Extraction:    每轮结束后，从对话中提取新记忆
    Consolidation: 记忆数 >= 10 时，合并去重、删除过时记忆

存储结构:
    .memory/
      MEMORY.md          ← 索引（每行一条，≤200 行）
      user-profile.md    ← 单条记忆（YAML frontmatter + markdown）
      project-facts.md
"""

import json
import re
from pathlib import Path
from anthropic import Anthropic


def _project_key(workdir: Path) -> str:
    """
    将项目路径转为安全的目录名，与 Claude Code 保持一致。
    D:\\Lab_WSPN\\claude_agent_impl → d--Lab-WSPN-claude-agent-impl
    """
    path_str = str(workdir.resolve()).lower()
    # 将盘符冒号和路径分隔符统一替换为 -
    key = re.sub(r'[:/\\]+', '-', path_str).strip('-')
    # 合并连续的 -
    key = re.sub(r'-+', '-', key)
    return key


def default_memory_dir(workdir: Path) -> Path:
    """~/.claude/projects/{project_key}/memory/"""
    return Path.home() / ".claude" / "projects" / _project_key(workdir) / "memory"


class MemoryManager:
    def __init__(
        self,
        client: Anthropic,
        model: str,
        memory_dir: Path | None = None,
        workdir: Path | None = None,
        max_selections: int = 5,
        consolidate_threshold: int = 10,
    ):
        self.client = client
        self.model = model
        self.memory_dir = memory_dir or default_memory_dir(workdir or Path.cwd())
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self.index_path = self.memory_dir / "MEMORY.md"
        self.max_selections = max_selections
        self.consolidate_threshold = consolidate_threshold

        # 启动时加载索引
        self._index: dict[str, dict] = {}  # slug -> {name, description, type, path}
        self._load_index()

    # ==================================================================
    # Selection — 按相关性选择记忆注入上下文
    # ==================================================================

    def select(self, messages: list, max_items: int | None = None) -> str:
        """
        从记忆目录中选择与当前对话相关的记忆。
        返回注入文本（空字符串表示无相关记忆）。

        策略: 关键词匹配（零 API 调用）
        """
        limit = max_items or self.max_selections
        if not self._index:
            return ""

        # 收集最近 3 条用户消息作为查询
        query_text = self._collect_recent_user_messages(messages, count=3)
        if not query_text.strip():
            return ""

        # 关键词匹配评分
        scored = []
        for slug, meta in self._index.items():
            score = self._relevance_score(query_text, meta)
            if score > 0:
                scored.append((score, slug, meta))

        # 按分数降序，取 top N
        scored.sort(reverse=True)
        selected = scored[:limit]

        if not selected:
            return ""

        # 加载选中的记忆内容
        parts = ["<memories>"]
        for score, slug, meta in selected:
            content = self._load_memory_content(slug)
            if content:
                parts.append(f"--- [{meta.get('type', 'unknown')}] {meta.get('name', slug)} ---")
                parts.append(content)
        parts.append("</memories>")

        return "\n\n".join(parts)

    def _collect_recent_user_messages(self, messages: list, count: int = 3) -> str:
        """收集最近 N 条用户消息的文本"""
        texts = []
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif getattr(block, "type", "") == "text":
                        texts.append(getattr(block, "text", ""))
            if len(texts) >= count:
                break
        return " ".join(texts)

    def _relevance_score(self, query: str, meta: dict) -> float:
        """关键词匹配评分（零 API）"""
        query_lower = query.lower()
        query_words = set(re.findall(r'\w+', query_lower))

        # 记忆的描述和名称
        name = meta.get("name", "").lower()
        desc = meta.get("description", "").lower()
        mem_words = set(re.findall(r'\w+', name + " " + desc))

        # 交集大小作为分数
        overlap = query_words & mem_words
        return len(overlap)

    # ==================================================================
    # Extraction — 从对话中提取新记忆
    # ==================================================================

    def extract(self, messages: list) -> list[str]:
        """
        从最近的对话中提取值得记住的信息。
        返回新创建的记忆 slug 列表。

        提取类型:
            user      — 用户偏好、角色、技能水平
            project   — 项目约束、目标、技术栈
            feedback  — 用户对 agent 行为的纠正
        """
        # 取最近 10 条消息
        recent = messages[-10:] if len(messages) > 10 else messages
        if len(recent) < 2:
            return []

        # 序列化为可读文本
        conversation = self._serialize_for_extraction(recent)
        if len(conversation.strip()) < 50:
            return []

        # 调用 LLM 提取记忆
        extracted = self._llm_extract(conversation)
        if not extracted:
            return []

        # 写入记忆文件
        new_slugs = []
        for item in extracted:
            slug = self._slugify(item.get("name", ""))
            if not slug:
                continue
            # 跳过已存在的同名记忆
            if slug in self._index:
                continue
            self._save_memory(slug, item)
            new_slugs.append(slug)

        # 更新索引
        if new_slugs:
            self._rebuild_index()

        return new_slugs

    def _serialize_for_extraction(self, messages: list) -> str:
        """将消息序列化为可读文本，供 LLM 提取"""
        lines = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str):
                lines.append(f"[{role}]: {content[:1500]}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "text":
                            lines.append(f"[{role}]: {block.get('text', '')[:1500]}")
                    elif hasattr(block, "text"):
                        lines.append(f"[{role}]: {block.text[:1500]}")
        return "\n".join(lines)

    def _llm_extract(self, conversation: str) -> list[dict] | None:
        """调用 LLM 从对话中提取记忆"""
        try:
            response = self.client.messages.create(
                model=self.model,
                system=(
                    "You are a memory extractor. Analyze the conversation and extract "
                    "worthwhile facts to remember for future sessions.\n\n"
                    "Extract ONLY non-obvious, durable information:\n"
                    "- user: role, expertise level, preferences, coding style\n"
                    "- project: tech stack, constraints, goals, architecture decisions\n"
                    "- feedback: corrections the user made to the agent's behavior\n\n"
                    "Return a JSON array. Each item:\n"
                    '{"name": "short-slug", "type": "user|project|feedback", '
                    '"description": "one-line summary", "body": "detailed markdown"}\n\n'
                    "Return [] if nothing worth remembering. Max 3 items."
                ),
                messages=[{"role": "user", "content": conversation}],
                max_tokens=2000,
            )
            text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "text", ""))

            # 提取 JSON 数组
            match = re.search(r'\[.*\]', text, re.DOTALL)
            if match:
                return json.loads(match.group())
            return None
        except Exception as e:
            print(f"\033[90m[memory] extraction failed: {e}\033[0m")
            return None

    # ==================================================================
    # Consolidation — 合并去重、删除过时记忆
    # ==================================================================

    def consolidate(self) -> int:
        """
        当记忆文件数 >= consolidate_threshold 时触发。
        用 LLM 合并重复、删除过时记忆。
        返回整理后的记忆总数。
        """
        if len(self._index) < self.consolidate_threshold:
            return len(self._index)

        # 加载所有记忆内容
        all_memories = []
        for slug, meta in self._index.items():
            content = self._load_memory_content(slug)
            all_memories.append({
                "slug": slug,
                "type": meta.get("type", "unknown"),
                "name": meta.get("name", slug),
                "description": meta.get("description", ""),
                "body": content or "",
            })

        # 调用 LLM 整理
        consolidated = self._llm_consolidate(all_memories)
        if not consolidated:
            return len(self._index)

        # 清空旧记忆，写入整理后的
        for slug in list(self._index.keys()):
            self._delete_memory(slug)

        for item in consolidated:
            slug = self._slugify(item.get("name", ""))
            if slug:
                self._save_memory(slug, item)

        self._rebuild_index()
        return len(self._index)

    def _llm_consolidate(self, memories: list[dict]) -> list[dict] | None:
        """调用 LLM 整理记忆"""
        memories_text = json.dumps(memories, ensure_ascii=False, indent=2)
        try:
            response = self.client.messages.create(
                model=self.model,
                system=(
                    "You are a memory consolidator. Given a list of memories, "
                    "merge duplicates, remove outdated ones, and keep only "
                    "the most important and durable facts.\n\n"
                    "Rules:\n"
                    "- Merge memories that say the same thing\n"
                    "- Remove memories that are no longer relevant\n"
                    "- Keep total count under 15\n"
                    "- Preserve the original format: name, type, description, body\n\n"
                    "Return a JSON array of consolidated memories."
                ),
                messages=[{"role": "user", "content": memories_text}],
                max_tokens=3000,
            )
            text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "text", ""))
            match = re.search(r'\[.*\]', text, re.DOTALL)
            if match:
                return json.loads(match.group())
            return None
        except Exception as e:
            print(f"\033[90m[memory] consolidation failed: {e}\033[0m")
            return None

    # ==================================================================
    # 文件操作
    # ==================================================================

    def _save_memory(self, slug: str, item: dict):
        """保存单条记忆到 .memory/{slug}.md"""
        name = item.get("name", slug)
        mem_type = item.get("type", "unknown")
        description = item.get("description", "")
        body = item.get("body", "")

        content = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {mem_type}\n"
            f"---\n\n"
            f"{body}\n"
        )

        path = self.memory_dir / f"{slug}.md"
        path.write_text(content, encoding="utf-8")

    def _delete_memory(self, slug: str):
        """删除单条记忆文件"""
        path = self.memory_dir / f"{slug}.md"
        if path.exists():
            path.unlink()

    def _load_memory_content(self, slug: str) -> str | None:
        """加载单条记忆的正文（跳过 frontmatter）"""
        path = self.memory_dir / f"{slug}.md"
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        _, body = self._parse_frontmatter(raw)
        return body if body.strip() else None

    def _load_index(self):
        """启动时从磁盘加载索引"""
        self._index.clear()
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            raw = md_file.read_text(encoding="utf-8")
            meta, _ = self._parse_frontmatter(raw)
            slug = md_file.stem
            self._index[slug] = {
                "name": meta.get("name", slug),
                "description": meta.get("description", ""),
                "type": meta.get("type", "unknown"),
            }
        # 重建 MEMORY.md 索引文件
        self._write_index_file()

    def _rebuild_index(self):
        """重新扫描目录并重建索引"""
        self._load_index()

    def _write_index_file(self):
        """写入 MEMORY.md 索引文件"""
        if not self._index:
            self.index_path.write_text("<!-- no memories yet -->\n", encoding="utf-8")
            return

        lines = ["# Memory Index\n"]
        for slug, meta in self._index.items():
            mem_type = meta.get("type", "?")
            desc = meta.get("description", "")
            name = meta.get("name", slug)
            lines.append(f"- [{name}]({slug}.md) — [{mem_type}] {desc}")

        self.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ==================================================================
    # 工具方法
    # ==================================================================

    @staticmethod
    def _parse_frontmatter(raw: str) -> tuple[dict, str]:
        """解析 YAML frontmatter + markdown body"""
        if not raw.startswith("---"):
            return {}, raw
        parts = raw.split("---", 2)
        if len(parts) < 3:
            return {}, raw
        meta_block = parts[1].strip()
        body = parts[2].strip()
        meta = {}
        for line in meta_block.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                meta[key.strip()] = val.strip()
        return meta, body

    @staticmethod
    def _slugify(text: str) -> str:
        """将文本转为安全的文件名 slug"""
        slug = re.sub(r'[^a-zA-Z0-9一-鿿]+', '-', text.lower()).strip('-')
        return slug[:50] if slug else ""

    @property
    def memory_count(self) -> int:
        return len(self._index)

    @property
    def has_memories(self) -> bool:
        return len(self._index) > 0
