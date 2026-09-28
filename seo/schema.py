"""The JSON contract. Every output file is validated against SiteReport before it is written."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "1.1"

CheckStatus = Literal["present", "missing", "partial", "not_checked", "error"]


class Check(BaseModel):
    """One SEO check. `status` is machine-readable; `note` is for humans.

    present = in place / healthy, partial = some pages or some aspects fail,
    missing = absent, not_checked = could not verify (see `reason`), error = check itself failed.
    """
    status: CheckStatus
    value: Any = None
    pages_checked: Optional[int] = None
    pages_affected: Optional[int] = None
    details: dict[str, Any] = Field(default_factory=dict)
    note: Optional[str] = None
    reason: Optional[str] = None


def not_checked(reason: str) -> Check:
    return Check(status="not_checked", reason=reason)


class TechnicalSEO(BaseModel):
    meta_title: Check
    meta_description: Check
    open_graph: Check
    twitter_card: Check
    h1: Check
    heading_hierarchy: Check
    image_alt_text: Check
    schema_markup: Check
    canonical: Check
    indexability: Check
    robots_txt: Check
    sitemap: Check
    https: Check
    mobile_viewport: Check
    lang: Check
    hreflang: Check
    analytics: Check
    cookie_banner: Check
    platform: Check
    broken_links: Check


class RobotsInfo(BaseModel):
    status: Literal["present", "missing", "error", "blocked", "not_checked"] = "not_checked"
    url: Optional[str] = None
    http_status: Optional[int] = None
    disallows_all: Optional[bool] = None
    blocks_googlebot: Optional[bool] = None
    blocks_this_crawler: Optional[bool] = None
    crawl_delay: Optional[float] = None
    sitemaps: list[str] = Field(default_factory=list)


class SitemapInfo(BaseModel):
    status: Literal["present", "missing", "invalid", "blocked", "not_checked"] = "not_checked"
    url: Optional[str] = None
    url_count: Optional[int] = None
    blog_url_count: Optional[int] = None
    is_index: Optional[bool] = None
    child_sitemaps: Optional[int] = None
    declared_in_robots: bool = False
    probe_checked: int = 0
    probe_dead: int = 0
    probe_dead_examples: list[str] = Field(default_factory=list)


class BrokenURL(BaseModel):
    url: str
    status_code: Optional[int] = None
    source: Optional[str] = None


class HttpsInfo(BaseModel):
    final_scheme: Optional[str] = None
    ssl_error: bool = False
    http_redirects_to_https: Optional[bool] = None
    hsts: bool = False


class BlockerCount(BaseModel):
    type: str
    count: int


class CrawlSummary(BaseModel):
    status: Literal["ok", "partial", "blocked", "failed"]
    reason: Optional[str] = None
    input_url: str
    start_url: Optional[str] = None
    final_url: Optional[str] = None
    final_domain: Optional[str] = None
    pages_attempted: int = 0
    pages_ok: int = 0
    pages_blocked: int = 0
    pages_failed: int = 0
    pages_skipped: int = 0
    blockers: list[BlockerCount] = Field(default_factory=list)
    broken_urls: list[BrokenURL] = Field(default_factory=list)
    sitemap_urls_fetched: int = 0
    flags: list[str] = Field(default_factory=list)
    budget_exceeded: Optional[str] = None
    stopped_early: Optional[str] = None
    robots: RobotsInfo = Field(default_factory=RobotsInfo)
    sitemap: SitemapInfo = Field(default_factory=SitemapInfo)
    https: Optional[HttpsInfo] = None
    duration_seconds: float = 0.0
    user_agent: str = ""


class PageFetch(BaseModel):
    url: str
    final_url: Optional[str] = None
    status_code: Optional[int] = None
    outcome: Literal["ok", "blocked", "failed", "skipped"]
    blocker: Optional[str] = None
    error: Optional[str] = None
    content_type: Optional[str] = None
    bytes: int = 0
    elapsed_ms: int = 0
    redirects: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    source: Optional[str] = None


class PageFacts(BaseModel):
    url: str
    page_type: str
    title: Optional[str] = None
    title_length: int = 0
    meta_description: Optional[str] = None
    meta_description_length: int = 0
    canonical: Optional[str] = None
    canonical_is_self: Optional[bool] = None
    meta_robots: Optional[str] = None
    x_robots_tag: Optional[str] = None
    noindex: bool = False
    lang: Optional[str] = None
    content_language: Optional[str] = None
    viewport: Optional[str] = None
    hreflang: list[str] = Field(default_factory=list)
    og: dict[str, str] = Field(default_factory=dict)
    twitter: dict[str, str] = Field(default_factory=dict)
    h1: list[str] = Field(default_factory=list)
    h1_count: int = 0
    h2: list[str] = Field(default_factory=list)
    h3_count: int = 0
    heading_skips: int = 0
    word_count: int = 0
    images_total: int = 0
    images_missing_alt: int = 0
    images_empty_alt: int = 0
    internal_links: int = 0
    external_links: int = 0
    nofollow_links: int = 0
    schema_types: list[str] = Field(default_factory=list)
    jsonld_blocks: int = 0
    jsonld_invalid: int = 0
    trackers: dict[str, list[str]] = Field(default_factory=dict)
    consent_vendor: Optional[str] = None
    platform: Optional[str] = None
    generator: Optional[str] = None
    text_excerpt: str = ""


class PageRecord(BaseModel):
    fetch: PageFetch
    facts: Optional[PageFacts] = None


class ContentStats(BaseModel):
    pages_analyzed: int = 0
    avg_word_count: int = 0
    thin_pages: int = 0
    page_types: dict[str, int] = Field(default_factory=dict)
    blog_detected: bool = False
    blog_posts_crawled: int = 0
    sitemap_blog_urls: Optional[int] = None


class ScoreItem(BaseModel):
    weight: float
    earned: float
    status: str


class Score(BaseModel):
    value: Optional[int] = None
    label: str
    coverage: float = 0.0
    pages_scored: int = 0
    confidence: Literal["high", "medium", "low", "none"] = "none"
    caveats: list[str] = Field(default_factory=list)
    breakdown: dict[str, ScoreItem] = Field(default_factory=dict)


PRIORITIES = ("high", "medium", "low")
EFFORTS = ("low", "medium", "high")
CATEGORIES = ("technical", "content", "keywords", "compliance", "social", "conversion", "performance", "other")
INTENTS = ("informational", "commercial", "transactional", "navigational", "unknown")
_SYNONYMS = {"critical": "high", "urgent": "high", "med": "medium", "moderate": "medium",
             "minor": "low", "easy": "low", "quick": "low", "hard": "high", "difficult": "high",
             "seo": "technical", "keyword": "keywords", "legal": "compliance", "ux": "conversion"}


def _norm(value: Any, allowed: tuple[str, ...], default: str) -> str:
    if isinstance(value, str):
        v = value.strip().lower()
        v = _SYNONYMS.get(v, v)
        if v in allowed:
            return v
        for a in allowed:
            if a in v:
                return a
    return default


class QuickWin(BaseModel):
    action: str
    priority: Literal["high", "medium", "low"] = "medium"
    effort: Literal["low", "medium", "high"] = "medium"
    category: Literal["technical", "content", "keywords", "compliance", "social",
                      "conversion", "performance", "other"] = "other"
    rationale: Optional[str] = None
    evidence: Optional[str] = None
    source: Literal["rule", "llm"] = "llm"
    related_check: Optional[str] = None

    @field_validator("priority", mode="before")
    @classmethod
    def _p(cls, v):
        return _norm(v, PRIORITIES, "medium")

    @field_validator("effort", mode="before")
    @classmethod
    def _e(cls, v):
        return _norm(v, EFFORTS, "medium")

    @field_validator("category", mode="before")
    @classmethod
    def _c(cls, v):
        return _norm(v, CATEGORIES, "other")


class KeywordSuggestion(BaseModel):
    keyword: str
    intent: Literal["informational", "commercial", "transactional", "navigational", "unknown"] = "unknown"
    rationale: Optional[str] = None
    source: Literal["llm_suggested"] = "llm_suggested"

    @field_validator("intent", mode="before")
    @classmethod
    def _i(cls, v):
        return _norm(v, INTENTS, "unknown")

    @field_validator("source", mode="before")
    @classmethod
    def _s(cls, v):
        return "llm_suggested"


class LLMOutput(BaseModel):
    """What the model must return. Lenient on shape, strict on meaning."""
    company: Optional[str] = None
    sector: Optional[str] = None
    summary: Optional[str] = None
    keyword_strategy: Optional[str] = None
    captured_keywords: list[str] = Field(default_factory=list)
    missed_keywords: list[KeywordSuggestion] = Field(default_factory=list)
    content_assessment: Optional[str] = None
    competitive_position: Optional[str] = None
    quick_wins: list[QuickWin] = Field(default_factory=list)

    @field_validator("captured_keywords", mode="before")
    @classmethod
    def _ck(cls, v):
        if not isinstance(v, list):
            return []
        out = []
        for item in v:
            if isinstance(item, dict):
                item = item.get("keyword") or item.get("term")
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out

    @field_validator("missed_keywords", mode="before")
    @classmethod
    def _mk(cls, v):
        if not isinstance(v, list):
            return []
        return [{"keyword": i} if isinstance(i, str) else i for i in v
                if isinstance(i, (str, dict))]

    @field_validator("quick_wins", mode="before")
    @classmethod
    def _qw(cls, v):
        if not isinstance(v, list):
            return []
        out = []
        for i in v:
            if isinstance(i, str):
                i = {"action": i}
            if isinstance(i, dict) and i.get("action"):
                i = {**i, "source": "llm"}
                out.append(i)
        return out


class LLMAnalysis(BaseModel):
    status: Literal["ok", "failed", "skipped"]
    reason: Optional[str] = None
    error_type: Optional[Literal["quota_exhausted", "auth_failed", "transient", "invalid_output", "other"]] = None
    model: Optional[str] = None
    company: Optional[str] = None
    sector: Optional[str] = None
    summary: Optional[str] = None
    keyword_strategy: Optional[str] = None
    captured_keywords: list[str] = Field(default_factory=list)
    missed_keywords: list[KeywordSuggestion] = Field(default_factory=list)
    content_assessment: Optional[str] = None
    competitive_position: Optional[str] = None


class Locale(BaseModel):
    country: Optional[str] = None
    country_source: Optional[Literal["tld", "content_language"]] = None
    html_lang: Optional[str] = None
    content_language: Optional[str] = None
    privacy_law: Optional[str] = None


class SiteReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    tool_version: str
    site: str
    company: Optional[str] = None
    sector: Optional[str] = None
    input_url: str
    analyzed_at: datetime
    analysis_seconds: float = 0.0
    crawl: CrawlSummary
    score: Score
    technical_seo: TechnicalSEO
    content_stats: ContentStats
    locale: Locale = Field(default_factory=Locale)
    llm_analysis: LLMAnalysis
    quick_wins: list[QuickWin] = Field(default_factory=list)
    pages: list[PageRecord] = Field(default_factory=list)
