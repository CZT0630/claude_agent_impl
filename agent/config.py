"""
配置模块 — 从 .env 加载 API 和模型配置
"""

import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Config:
    api_key: str
    model: str
    base_url: str | None
    workdir: Path
    max_tokens: int = 8000

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "Config":
        load_dotenv(env_path or Path(".env"), override=True)

        if os.getenv("ANTHROPIC_BASE_URL"):
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        model = os.environ.get("MODEL_ID", "claude-sonnet-4-6")
        base_url = os.getenv("ANTHROPIC_BASE_URL")
        workdir = Path.cwd()
        max_tokens = int(os.getenv("MAX_TOKENS", "8000"))

        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in .env")

        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            workdir=workdir,
            max_tokens=max_tokens,
        )
