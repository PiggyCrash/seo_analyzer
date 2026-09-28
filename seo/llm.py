"""LLM layer (any OpenAI-compatible API: MiniMax, Cerebras, ...). Interprets facts; never measures them."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from typing import Optional

from pydantic import ValidationError

from .config import LLMConfig
from .schema import ContentStats, CrawlSummary, LLMOutput, Locale, PageRecord, QuickWin, TechnicalSEO

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    error_type = "unavailable"


class LLMQuotaExhausted(LLMUnavailable):
    """Plan/credit/daily quota used up. Retrying won't help until it resets or is topped up."""
    error_type = "quota_exhausted"


class LLMAuthFailed(LLMUnavailable):
    """Key rejected (wrong key, wrong region endpoint, revoked)."""
    error_type = "auth_failed"


_QUOTA_RE = re.compile(r"usage limit|quota|insufficient|credit|balance|billing|payment|per day|daily|"
                       r"per month|monthly|limit reached|\b(tpd|rpd)\b|\(2056\)|\(1008\)", re.I)
_TRANSIENT_RE = re.compile(r"per minute|per second|\b(rpm|tpm|rps)\b|\(1002\)", re.I)


def classify_error(exc: Exception) -> str:
    """-> quota_exhausted | auth_failed | transient | other"""
    import openai
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return "transient"
    if not isinstance(exc, openai.APIStatusError):
        return "other"
    code, text = exc.status_code, str(exc)
    if code in (401, 403):
        return "auth_failed"
    if code == 402:
        return "quota_exhausted"
    if code == 429:
        if _TRANSIENT_RE.search(text):
            return "transient"
        return "quota_exhausted" if _QUOTA_RE.search(text) else "transient"
    if code >= 500:
        return "transient"
    return "other"


def _api_message(exc: Exception) -> str:
    """Short human message from an OpenAI-style error body, falling back to str(exc)."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    return str(exc)[:300]


SYSTEM_PROMPT = """You are a senior SEO analyst. You receive a JSON digest of facts measured by a crawler for one website.

Rules:
- The digest facts are ground truth. Never contradict them and never invent technical findings.
- `rule_based_quick_wins` were already generated from the facts. Do NOT repeat or rephrase them. Your quick_wins must focus on content, keywords, positioning, conversion and strategy, or issues the rules cannot express.
- You have NO search-volume, ranking or backlink data. Keywords are hypotheses inferred from the site's content; phrase rationales accordingly.
- Write all prose (summary, assessments, actions, rationales) in English. Only keywords go in the audience's language.
- Use `context`. Today is `context.analysis_date`: never put a past year in a keyword or recommendation. Write for `context.country` and the audience's language (`context.content_language`); suggest keywords in the language that audience searches in. Cite only `context.privacy_law` or other laws/regulators of that country, never another country's.
- `context.sample` says how many pages you saw out of how many the sitemap lists. When the sample is small, qualify site-wide statements ("in the N pages sampled") instead of asserting them for the whole site.
- Respect what the facts say does NOT exist. If `content_stats.blog_detected` is false, do not treat the site as having blog posts; "create a blog/news section" is a separate, high-effort project.
- Every quick win MUST include `evidence`: the specific URL, title, heading, excerpt phrase or measured value in the digest that motivates it. The evidence must actually support the action (e.g. do not recommend translating text that is already in the audience's language). Quick wins without concrete evidence are discarded.
- Do not propose features the site gives no sign of offering (live chat, appointment booking, e-commerce, etc.). Suggestions must fit what this organisation actually does, as shown in the digest.
- effort: low = under a day of work, medium = a few days, high = weeks or a new site section. Rank honestly.
- Be specific to THIS site and its niche. No generic filler.
- Output ONLY one JSON object. No markdown, no code fences, no commentary.

Required JSON shape:
{
  "company": "company/brand name or null",
  "sector": "short sector description",
  "summary": "2-3 sentence overall SEO assessment, mentioning the sample size if it is small",
  "keyword_strategy": "how the site currently targets search, if at all",
  "captured_keywords": ["3-8 keywords/topics the site already targets, evidenced by titles, headings or copy"],
  "missed_keywords": [{"keyword": "string", "intent": "informational|commercial|transactional|navigational", "rationale": "why it fits this site"}],
  "content_assessment": "quality, depth and originality of the content",
  "competitive_position": "likely organic position and opportunity in its niche",
  "quick_wins": [{"action": "string", "priority": "high|medium|low", "effort": "low|medium|high", "category": "content|keywords|technical|compliance|social|conversion|other", "rationale": "string", "evidence": "string"}]
}
Give 5-10 missed_keywords and 3-7 quick_wins."""

_YEAR = re.compile(r"\b(19|20)\d{2}\b")


RULE_TOPICS = {
    "meta_title": re.compile(r"<title>|\btitle tags?\b|\bmeta titles?\b|\btitles?\b.*\b(unique|length|characters|translate)", re.I),
    "meta_description": re.compile(r"meta descriptions?", re.I),
    "h1": re.compile(r"\bh1\b", re.I),
    "image_alt_text": re.compile(r"\balt (text|attributes?)\b", re.I),
    "robots_txt": re.compile(r"robots\.txt", re.I),
    "schema_markup": re.compile(r"organization (schema|json-ld)|website (schema|json-ld)", re.I),
    "lang": re.compile(r"lang attribute|<html lang", re.I),
    "cookie_banner": re.compile(r"consent banner|cookie banner", re.I),
    "canonical": re.compile(r"canonical", re.I),
    "open_graph": re.compile(r"open graph|og:", re.I),
    "broken_links": re.compile(r"dead (urls?|links?)|broken links?|\b404\b", re.I),
}


def clean_output(out: LLMOutput, today: date, rule_checks: frozenset[str] = frozenset()) -> LLMOutput:
    """Drop what the prompt forbids but models still produce: evidence-free wins, stale-year keywords,
    and wins that restate a rule-based quick win."""
    def stale(text: str) -> bool:
        return any(int(m.group(0)) < today.year for m in _YEAR.finditer(text or ""))

    def repeats_rule(action: str) -> bool:
        return any(RULE_TOPICS[c].search(action) for c in rule_checks if c in RULE_TOPICS)

    wins = [q for q in out.quick_wins
            if (q.evidence or "").strip() and not stale(q.action) and not repeats_rule(q.action)]
    kws = [k for k in out.missed_keywords if not stale(k.keyword)]
    dropped = len(out.quick_wins) - len(wins), len(out.missed_keywords) - len(kws)
    if any(dropped):
        log.info("dropped %d LLM quick win(s) (no evidence, stale year or repeats a rule), %d stale keyword(s)",
                 *dropped)
    return out.model_copy(update={"quick_wins": wins, "missed_keywords": kws})


def build_digest(site: str, crawl: CrawlSummary, tech: TechnicalSEO, content: ContentStats, locale: Locale,
                 records: list[PageRecord], rule_wins: list[QuickWin], max_chars: int) -> dict:
    checks = {}
    for name, c in tech:
        entry = {"status": c.status}
        if c.value not in (None, [], ""):
            entry["value"] = c.value
        if c.pages_affected:
            entry["pages_affected"] = c.pages_affected
        slim = {k: v for k, v in c.details.items() if not k.startswith("examples") and v not in (None, [], {}, 0, False)}
        if slim:
            entry["details"] = slim
        checks[name] = entry

    pages, excerpts = [], []
    for r in records:
        f = r.facts
        if not f:
            continue
        pages.append({"url": f.url, "type": f.page_type, "lang": f.lang, "content_language": f.content_language,
                      "title": f.title, "meta_description": f.meta_description,
                      "h1": f.h1[:2], "h2": f.h2[:8], "word_count": f.word_count,
                      "internal_links": f.internal_links, "schema_types": f.schema_types})
        excerpts.append(f.text_excerpt)

    digest = {
        "site": site,
        "context": {
            "analysis_date": datetime.now(timezone.utc).date().isoformat(),
            "country": locale.country, "content_language": locale.content_language,
            "html_lang": locale.html_lang, "privacy_law": locale.privacy_law,
            "sample": {"pages_analyzed": content.pages_analyzed, "sitemap_urls": crawl.sitemap.url_count,
                       "broken_urls_found": len(crawl.broken_urls)},
        },
        "crawl": {"status": crawl.status, "pages_ok": crawl.pages_ok, "flags": crawl.flags,
                  "blockers": [b.model_dump() for b in crawl.blockers]},
        "technical_checks": checks,
        "content_stats": content.model_dump(),
        "rule_based_quick_wins": [q.action for q in rule_wins],
        "pages": pages,
    }

    length = 1200
    while True:
        for p, ex in zip(pages, excerpts):
            p["excerpt"] = ex[:length]
        if len(json.dumps(digest, ensure_ascii=False)) <= max_chars or length <= 120:
            break
        length = int(length * 0.6)
    if len(json.dumps(digest, ensure_ascii=False)) > max_chars:
        for p in pages:
            p["h2"] = p["h2"][:3]
    while len(json.dumps(digest, ensure_ascii=False)) > max_chars and len(pages) > 3:
        pages.pop()
    return digest


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.I | re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model response")
    return json.loads(text[start:end + 1])


class LLMAnalyzer:
    def __init__(self, cfg: LLMConfig):
        key = os.getenv(cfg.api_key_env)
        if not key:
            raise LLMUnavailable(f"{cfg.api_key_env} is not set (add it to .env)")
        from openai import AsyncOpenAI
        self.cfg = cfg
        self.client = AsyncOpenAI(api_key=key, base_url=cfg.base_url, max_retries=0, timeout=cfg.timeout)
        self._json_mode = cfg.json_mode
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._blocked: Optional[LLMUnavailable] = None
        self._blocked_at = 0.0

    @property
    def problem(self) -> Optional[LLMUnavailable]:
        """The quota/auth error that currently stops LLM calls, or None."""
        if self._blocked and time.monotonic() - self._blocked_at >= self.cfg.quota_cooldown_seconds:
            self._blocked = None
        return self._blocked

    def _trip(self, exc: Exception, kind: str) -> LLMUnavailable:
        cls = LLMQuotaExhausted if kind == "quota_exhausted" else LLMAuthFailed
        err = cls(f"{_api_message(exc)} [HTTP {getattr(exc, 'status_code', '?')}, {self.cfg.base_url}]")
        if self._blocked is None:
            log.error("LLM %s: %s -- skipping the LLM for the next %ds",
                      kind.replace("_", " "), err, self.cfg.quota_cooldown_seconds)
        self._blocked, self._blocked_at = err, time.monotonic()
        return err

    async def _create(self, **kwargs):
        attempts = 1 + max(0, self.cfg.max_retries)
        for attempt in range(attempts):
            if self.problem:
                raise self.problem
            await self._throttle()
            try:
                return await self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                kind = classify_error(exc)
                if kind in ("quota_exhausted", "auth_failed"):
                    raise self._trip(exc, kind) from exc
                if kind != "transient" or attempt == attempts - 1:
                    raise
                delay = min(30.0, 2.0 * 2 ** attempt)
                retry_after = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
                if retry_after and retry_after.replace(".", "", 1).isdigit():
                    delay = min(60.0, float(retry_after))
                log.warning("LLM transient error (%s), retry %d/%d in %.0fs",
                            _api_message(exc)[:120], attempt + 1, attempts - 1, delay)
                await asyncio.sleep(delay)

    async def _throttle(self) -> None:
        interval = 60.0 / max(1, self.cfg.requests_per_minute)
        async with self._lock:
            wait = self._last + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def _chat(self, messages: list[dict]) -> str:
        from openai import BadRequestError
        kwargs = dict(model=self.cfg.model, messages=messages, temperature=self.cfg.temperature,
                      max_tokens=self.cfg.max_tokens)
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._create(**kwargs)
        except BadRequestError as exc:
            if self._json_mode and "response_format" in str(exc).lower():
                log.warning("model rejected JSON mode, retrying without it")
                self._json_mode = False
                kwargs.pop("response_format")
                resp = await self._create(**kwargs)
            else:
                raise
        return resp.choices[0].message.content or ""

    async def analyze(self, digest: dict, rule_checks: frozenset[str] = frozenset()) -> LLMOutput:
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(digest, ensure_ascii=False)}]
        raw = await self._chat(messages)
        today = date.fromisoformat(digest["context"]["analysis_date"])
        try:
            return clean_output(LLMOutput.model_validate(_extract_json(raw)), today, rule_checks)
        except (ValueError, ValidationError) as exc:
            log.warning("invalid LLM JSON, asking for a correction: %s", str(exc)[:200])
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"That response was invalid: {str(exc)[:800]}\n"
                                                     "Return ONLY the corrected JSON object."}]
            raw = await self._chat(messages)
            return clean_output(LLMOutput.model_validate(_extract_json(raw)), today, rule_checks)
