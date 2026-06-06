"""
技能加载器 — 两级按需知识注入

Layer 1 (始终在，便宜): SYSTEM prompt 注入技能目录 (~100 tokens/skill)
Layer 2 (按需，昂贵):   Agent 调用 load_skill → 完整 SKILL.md 内容 (~2000 tokens/skill)
"""

from pathlib import Path


class SkillLoader:
    def __init__(self, skills_dir: Path | None = None):
        self.skills_dir = skills_dir or Path.cwd() / ".claude" / "skills"
        self._registry: dict[str, dict] = {}
        self._scan()

    def _scan(self):
        """启动时扫描 skills/ 目录，构建注册表"""
        for d in sorted(self.skills_dir.iterdir()):
            manifest = d / "SKILL.md"
            if not manifest.is_file():
                continue
            raw = manifest.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(raw)
            name = meta.get("name", d.name)
            self._registry[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "content": raw,
            }

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

    def list_skills(self) -> str:
        """Layer 1: 返回技能目录（注入 SYSTEM prompt，便宜）"""
        if not self._registry:
            return "(no skills available)"
        lines = []
        for name, skill in self._registry.items():
            lines.append(f"  - {name}: {skill['description']}")
        return "\n".join(lines)

    def load_skill(self, name: str) -> str:
        """Layer 2: 返回完整技能内容（按需加载，昂贵）"""
        skill = self._registry.get(name)
        if not skill:
            available = ", ".join(self._registry.keys()) or "(none)"
            return f"Skill not found: {name}\nAvailable: {available}"
        return skill["content"]

    @property
    def has_skills(self) -> bool:
        return len(self._registry) > 0


# --- Tool schema ---

LOAD_SKILL_SCHEMA = {
    "name": "load_skill",
    "description": "Load the full content of a skill by name. Use this to get detailed instructions for a specific task type.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (e.g. 'code-review', 'agent-builder').",
            },
        },
        "required": ["name"],
    },
}


def make_load_skill_handler(loader: SkillLoader):
    """构建 load_skill 工具的 handler"""
    def run_load_skill(name: str) -> str:
        return loader.load_skill(name)
    return run_load_skill
