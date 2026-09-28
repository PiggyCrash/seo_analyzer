"""Settings: defaults here, overridden by config.yaml, secrets from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class CrawlConfig:
    max_pages: int = 15
    max_fetch_attempts: int = 40
    sitemap_probe: int = 10
    per_domain_concurrency: int = 2
    delay_seconds: float = 1.0
    request_timeout: float = 15.0
    max_redirects: int = 5
    max_retries: int = 2
    site_time_budget: float = 180.0
    max_page_bytes: int = 5_000_000
    max_site_bytes: int = 50_000_000
    user_agent: str = "SEOAnalyzerBot/1.0 (+mailto:you@example.com)"
    include_subdomains: bool = False
    respect_robots: bool = True


@dataclass
class LLMConfig:
    enabled: bool = True
    base_url: str = "https://api.minimax.io/v1"
    model: str = "MiniMax-M2.7"
    api_key_env: str = "MINIMAX_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 8000
    timeout: float = 180.0
    max_retries: int = 3
    quota_cooldown_seconds: float = 900.0
    json_mode: bool = True
    requests_per_minute: int = 20
    min_ok_pages: int = 3
    max_digest_chars: int = 14000


@dataclass
class StorageConfig:
    output_dir: str = "output"
    cache_dir: str = "cache"
    skip_if_fresh_hours: float = 24.0
    include_pages: bool = True


@dataclass
class RunConfig:
    site_concurrency: int = 3


@dataclass
class Settings:
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    run: RunConfig = field(default_factory=RunConfig)


def _apply(target, data: dict) -> None:
    names = {f.name for f in fields(target)}
    for key, value in data.items():
        if key in names:
            setattr(target, key, value)


def load_settings(path: Optional[str | Path] = None) -> Settings:
    load_dotenv(ROOT_DIR / ".env")
    settings = Settings()
    cfg_path = Path(path) if path else ROOT_DIR / "config.yaml"
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        for section in ("crawl", "llm", "storage", "run"):
            if isinstance(data.get(section), dict):
                _apply(getattr(settings, section), data[section])
    if os.getenv("LLM_MODEL"):
        settings.llm.model = os.environ["LLM_MODEL"]
    return settings
