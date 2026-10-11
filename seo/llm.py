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
from .locale import detect_language
from .schema import ContentStats, CrawlSummary, Finding, LLMOutput, Locale, PageRecord, QuickWin, TechnicalSEO

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
- First decide the site's ROLE from the digest (e.g. corporate / investor-relations hub, online store, local service business, SaaS, media). Keywords and wins must fit that role. `context.related_domains` lists the non-social domains the homepage links to (often subsidiaries, shops, portals). When a related domain clearly serves an intent (a pharmacy/shop domain = retail and product queries, a lab domain = lab services, a careers/e-procurement portal = those queries), do NOT suggest keywords, pages or wins for that intent on this site; name the domain in keyword_strategy instead.
- Prefer wins that improve pages that already exist. The homepage entry lists `existing_paths` (pages linked from the homepage): never propose creating a page, hub or section that already appears there; improve it instead. Creating a genuinely new page is effort "high".
- Do not invent facts about the organisation (parent company, ticker, founding year, locations) that are not in the digest excerpts or clearly common knowledge for this exact company. A subsidiary of the site's own group is not a competitor.
- `findings` and `technical_checks` list defects already measured (soft-404s, sitemap prefix bugs, placeholder content, leaked headers, JS-only counters...). Use them to explain impact in summary/content_assessment, but do not turn them into quick wins again.
- captured_keywords must be search phrases a user would type, evidenced by titles, headings or copy. Never list bare navigation labels ("Profil", "Contact Center", "Home").
- Pages with `empty_template: true` are empty CMS shells: never count their text as content.
- In image checks, `images_missing_alt` counts images with no alt attribute; `images_empty_alt` counts alt="" (valid for decoration). Never call empty-alt images "missing alt text".
- Use concrete facts from the excerpts (founding year, figures, product lines, subsidiaries, licences, locations, stock ticker) in content_assessment and in entity/schema suggestions.
- competitive_position: name 2-4 real, well-known competitors in this country and niche, and say which queries are realistically winnable. You have no ranking data: phrase it as an assessment, not a measurement.
- If a heading or title in the digest contains an obvious misspelling, you may add a quick win to fix it, quoting the exact text as evidence.
- Quick wins must name the exact page, element, text or entity involved (e.g. "Add Organization JSON-LD with tickerSymbol X and parentOrganization Y"), not a generic category.
- effort: low = under a day of work, medium = a few days, high = weeks or a new site section. Rank honestly.
- Be specific to THIS site and its niche. No generic filler.
- Output ONLY one JSON object. No markdown, no code fences, no commentary. Inside string values never use a bare double quote: quote titles and phrases with single quotes ('like this').
- Keywords are bare search phrases: no explanations, page paths or parentheses inside keyword strings.

Required JSON shape:
{
  "company": "company/brand name or null",
  "sector": "short sector description",
  "summary": "2-3 sentence overall SEO assessment, mentioning the sample size if it is small",
  "executive_summary": ["exactly 3 bullets, max 25 words each: the three problems or opportunities that matter most for THIS business, most important first, each naming the page, element or value"],
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
    "schema_markup": re.compile(r"organization (schema|json-ld)|website (schema|json-ld)|json-ld:? organization"
                                r"|organization\b.{0,30}\bjson-ld", re.I),
    "lang": re.compile(r"lang attribute|<html lang", re.I),
    "cookie_banner": re.compile(r"consent banner|cookie banner", re.I),
    "canonical": re.compile(r"canonical", re.I),
    "open_graph": re.compile(r"open graph|og:", re.I),
    "broken_links": re.compile(r"dead (urls?|links?)|broken links?|\b404\b", re.I),
    "not_found_handling": re.compile(r"\b404\b|soft.?404|unknown urls?|empty (template|shell)", re.I),
    "sitemap": re.compile(r"sitemap", re.I),
    "hreflang": re.compile(r"hreflang", re.I),
    "heading_hierarchy": re.compile(r"\bh[1-6]\b.*(hierarch|outline|level)|heading (levels?|hierarchy)", re.I),
    "zero_counters": re.compile(r"counters?|statistics? (render|show)", re.I),
    "name_mismatch": re.compile(r"spelling|misspell", re.I),
    "leaky_headers": re.compile(r"(debug|response) header", re.I),
    "legacy_scripts": re.compile(r"html5shiv|respond\.js|maxcdn|polyfill", re.I),
    "no_google_analytics": re.compile(r"google analytics|\bga4\b|tag manager", re.I),
    "placeholder_content": re.compile(r"placeholder", re.I),
    "stale_copyright": re.compile(r"copyright", re.I),
    "typos": re.compile(r"typo|misspell", re.I),
    "internal_linking": re.compile(r"internal link|read more|anchor text|learn more", re.I),
    "broken_email_links": re.compile(r"mailto|email link", re.I),
    "thin_content": re.compile(r"thin (pages?|content)|expand .{0,40}pages?|\b\d{3,4}\+? words\b", re.I),
    "schema_org_fields": re.compile(r"sameas|organization (json-ld|schema|markup)", re.I),
    "schema_faq": re.compile(r"faq(page)? (schema|json-ld|markup)|faqpage", re.I),
    "duplicate_slugs": re.compile(r"duplicate (pages?|content|urls?)|-0\b", re.I),
    "nav_headings": re.compile(r"(hidden|block|menu) (titles?|labels?|headings?)", re.I),
}


def covered_topics(rule_wins: list[QuickWin]) -> frozenset[str]:
    """Topics the rule-based wins already cover: their own check plus any topic their text mentions
    (e.g. the lang win also asks for hreflang)."""
    topics = {q.related_check for q in rule_wins if q.related_check}
    topics |= {t for t, rx in RULE_TOPICS.items() for q in rule_wins if q.source == "rule" and rx.search(q.action)}
    return frozenset(topics)


SOCIAL_DOMAINS = frozenset({"facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com", "youtube.com",
                            "tiktok.com", "wa.me", "api.whatsapp.com", "t.me", "pinterest.com", "google.com",
                            "maps.google.com", "goo.gl", "bit.ly"})


# Rule wins the LLM may REPLACE with a more specific version (it knows the entity: legal name, ticker, parent...).
MERGEABLE = frozenset({"schema_markup"})
_DASHES = re.compile(r"[‐-―−]")


_SCHEMA_KEYS = {"company", "sector", "summary", "keyword_strategy", "captured_keywords", "missed_keywords",
                "content_assessment", "competitive_position", "quick_wins"}


def output_problems(out: LLMOutput) -> list[str]:
    """Structural and language problems worth one correction round."""
    problems = []
    junk = [k for k in out.captured_keywords + [m.keyword for m in out.missed_keywords]
            if k.strip(" :,") in _SCHEMA_KEYS or not k.strip(" :,\"") or any(ch in k for ch in '{}[]"')
            or len(k) > 90]
    if junk:
        problems.append(f"keyword lists contain JSON fragments or sentences instead of keywords: {junk[:3]}")
    prose = " ".join(filter(None, (out.summary, out.keyword_strategy, out.content_assessment, out.competitive_position)))
    lang = detect_language(prose)
    if lang and lang != "en":
        problems.append(f"prose fields are written in '{lang}', they must be in English (only keywords may be "
                        "in the audience's language)")
    if not out.competitive_position or not out.summary:
        problems.append("summary and competitive_position are required")
    return problems


def served_elsewhere(site: str, related_domains: list[str]) -> frozenset[str]:
    """Intent tokens owned by sister sites that share the brand: kimiafarma.co.id links to
    kimiafarmaapotek.co.id -> 'apotek', labkimiafarma.co.id -> 'lab', eproc.kimiafarma.co.id -> 'eproc'."""
    brand = site.split(".")[0].lower()
    tokens = set()
    for d in related_domains:
        labels = d.lower().split(".")
        if d.lower().endswith("." + site.lower()):
            rest = labels[0]
        elif brand in labels[0] and labels[0] != brand:
            rest = labels[0].replace(brand, "")
        else:
            continue
        rest = re.sub(r"[^a-z]", "", rest)
        if len(rest) >= 3:
            tokens.add(rest)
    return frozenset(tokens)


def _mentions(text: str, tokens: frozenset[str]) -> bool:
    words = re.findall(r"[^\W\d_]+", (text or "").lower().replace("-", ""))
    return any(w.startswith(t) for w in words for t in tokens)


_CREATE = re.compile(r"^(?:create|build|add|launch|stand up|publish|set up)\b(?P<subject>.{0,80}?)\b"
                     r"(?:page|hub|section|portal|microsite)s?\b", re.I)
_GENERIC_WORDS = {"dedicated", "landing", "capability", "capabilities", "solution", "solutions", "service", "services",
                  "company", "section", "content", "detailed", "specific", "separate", "single", "proper",
                  "the", "and", "for", "with", "new", "our", "its", "all", "each", "one", "that", "page", "pages",
                  "standalone", "keyword", "optimized", "optimised", "focused", "comparison", "vertical", "verticals",
                  "industry", "industries", "regional", "short", "clear", "strong", "combined", "central", "filter"}


def proposes_existing_page(action: str, existing_paths: list[str]) -> bool:
    """'Add an Investor Relations hub page' when /investors is already linked from the homepage."""
    m = _CREATE.search(action.strip())
    if not m or not existing_paths:
        return False
    explicit = re.findall(r"(/[\w\-/]+)", m.group(0))
    if explicit:
        return any(p.rstrip("/") in existing_paths for p in explicit)
    words = {w for w in re.findall(r"[a-z]{3,}", m.group("subject").lower()) if w not in _GENERIC_WORDS}
    tokens = {t for p in existing_paths for t in re.split(r"[/\-_]", p.lower()) if len(t) >= 3}
    # Short words must match a path segment exactly ('lng'); longer ones by stem ('investor' ~ 'investors').
    return any((w == t) if len(w) < 6 else (w[:6] == t[:6]) for w in words for t in tokens)


def clean_output(out: LLMOutput, today: date, rule_checks: frozenset[str] = frozenset(),
                 nav_labels: frozenset[str] = frozenset(), elsewhere: frozenset[str] = frozenset(),
                 existing_paths: list[str] = ()) -> LLMOutput:
    """Drop what the prompt forbids but models still produce: evidence-free wins, stale-year keywords,
    wins that restate a rule-based quick win, navigation labels passed off as keywords, and keywords/wins
    for an intent a sister site of the same brand already serves."""
    def stale(text: str) -> bool:
        return any(int(m.group(0)) < today.year for m in _YEAR.finditer(text or ""))

    def topic(action: str) -> Optional[str]:
        """The rule topic an LLM win overlaps; a non-mergeable overlap wins so the result is deterministic."""
        text = _DASHES.sub("-", action)
        hits = sorted(c for c in rule_checks if c in RULE_TOPICS and RULE_TOPICS[c].search(text))
        return next((c for c in hits if c not in MERGEABLE), hits[0] if hits else None)

    wins = []
    for q in out.quick_wins:
        q = q.model_copy(update={"action": _DASHES.sub("-", q.action)})
        t = topic(q.action)
        if not (q.evidence or "").strip() or stale(q.action) or (t and t not in MERGEABLE) \
                or _mentions(q.action, elsewhere) or proposes_existing_page(q.action, list(existing_paths)):
            continue
        wins.append(q.model_copy(update={"related_check": t}) if t else q)
    def junk(k: str) -> bool:
        return k.strip(" :,") in _SCHEMA_KEYS or not k.strip(" :,\"") or any(ch in k for ch in '{}[]"') or len(k) > 90

    kws = [k for k in out.missed_keywords
           if not stale(k.keyword) and not _mentions(k.keyword, elsewhere) and not junk(k.keyword)]
    # Models sometimes annotate a keyword with its evidence: "LNG EPC solutions (page /solutions/lng)".
    strip_note = lambda k: re.sub(r"\s*\([^)]*\)\s*$", "", k).strip()  # noqa: E731
    captured = [strip_note(k) for k in out.captured_keywords]
    captured = [k for k in captured if k and k.casefold() not in nav_labels and not k.isupper() and not junk(k)]
    kws = [k.model_copy(update={"keyword": strip_note(k.keyword)}) for k in kws]
    dropped = len(out.quick_wins) - len(wins), len(out.missed_keywords) - len(kws), \
        len(out.captured_keywords) - len(captured)
    if any(dropped):
        log.info("dropped %d LLM quick win(s) (no evidence, stale year or repeats a rule), %d stale keyword(s), "
                 "%d navigation label(s)", *dropped)
    return out.model_copy(update={"quick_wins": wins, "missed_keywords": kws, "captured_keywords": captured})


def build_digest(site: str, crawl: CrawlSummary, tech: TechnicalSEO, content: ContentStats, locale: Locale,
                 records: list[PageRecord], rule_wins: list[QuickWin], max_chars: int,
                 findings: list[Finding] = ()) -> dict:
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

    home = records[0].facts if records and records[0].facts else None
    related = [d for d in (home.external_domains if home else []) if d not in SOCIAL_DOMAINS][:12]

    pages, excerpts = [], []
    for r in records:
        f = r.facts
        if not f:
            continue
        if f.empty_template:
            pages.append({"url": f.url, "empty_template": True, "title": f.title})
            excerpts.append(None)
            continue
        entry = {"url": f.url, "type": f.page_type, "lang": f.lang, "content_language": f.content_language,
                 "title": f.title, "meta_description": f.meta_description,
                 "h1": f.h1[:2], "h2": f.h2[:8], "word_count": f.word_count,
                 "internal_links": f.internal_links, "schema_types": f.schema_types}
        if f.page_type == "home":
            entry["top_heading"] = f.top_heading
            entry["minor_headings"] = f.minor_headings[:25]
            entry["existing_paths"] = f.internal_paths[:50]
        pages.append(entry)
        excerpts.append(f.text_excerpt)
    checks = _slim(checks)

    digest = {
        "site": site,
        "context": {
            "analysis_date": datetime.now(timezone.utc).date().isoformat(),
            "country": locale.country, "content_language": locale.content_language,
            "html_lang": locale.html_lang, "privacy_law": locale.privacy_law,
            "sample": {"pages_analyzed": content.pages_analyzed, "sitemap_urls": crawl.sitemap.url_count,
                       "broken_urls_found": len(crawl.broken_urls)},
            "related_domains": related,
        },
        "crawl": {"status": crawl.status, "pages_ok": crawl.pages_ok, "flags": crawl.flags,
                  "blockers": [b.model_dump() for b in crawl.blockers]},
        "technical_checks": checks,
        "findings": [f.message for f in findings],
        "content_stats": content.model_dump(),
        "rule_based_quick_wins": [q.action for q in rule_wins],
        "pages": pages,
    }

    length = 1200
    while True:
        for p, ex in zip(pages, excerpts):
            if ex is not None:
                p["excerpt"] = ex[:length]
        if len(json.dumps(digest, ensure_ascii=False)) <= max_chars or length <= 120:
            break
        length = int(length * 0.6)
    if len(json.dumps(digest, ensure_ascii=False)) > max_chars:
        for p in pages:
            if "h2" in p:
                p["h2"] = p["h2"][:3]
    size = lambda: len(json.dumps(digest, ensure_ascii=False))  # noqa: E731
    while size() > max_chars and len(pages) > 3:
        pages.pop()
    # Last resort for huge sites: shorter excerpts, fewer paths, then no excerpts at all.
    if size() > max_chars:
        for p in pages:
            if "existing_paths" in p:
                p["existing_paths"] = p["existing_paths"][:20]
            if "excerpt" in p:
                p["excerpt"] = p["excerpt"][:200]
    while size() > max_chars and len(pages) > 1:
        pages.pop()
    return digest


def _slim(value, depth: int = 0):
    """Bound every list and string in the check details (a robots.txt can list 500+ sitemaps)."""
    if isinstance(value, dict):
        return {k: _slim(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        items = [_slim(v, depth + 1) for v in value[:6]]
        return items + [f"... {len(value) - 6} more"] if len(value) > 6 else items
    if isinstance(value, str) and len(value) > 240:
        return value[:240] + "…"
    return value


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.I | re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model response")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        # Text before/after the object ("Extra data"): decode the first complete object that has our keys.
        decoder = json.JSONDecoder()
        for m in re.finditer(r"\{", text):
            try:
                obj, _ = decoder.raw_decode(text, m.start())
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.keys() & {"summary", "quick_wins", "keyword_strategy"}:
                return obj
        raise exc


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

    async def _chat(self, messages: list[dict], usage: Optional[dict] = None) -> str:
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
        if usage is not None:
            u = getattr(resp, "usage", None)
            details = getattr(u, "completion_tokens_details", None)
            usage["calls"] = usage.get("calls", 0) + 1
            for key, val in (("prompt_tokens", getattr(u, "prompt_tokens", 0)),
                             ("completion_tokens", getattr(u, "completion_tokens", 0)),
                             ("reasoning_tokens", getattr(details, "reasoning_tokens", 0)),
                             ("total_tokens", getattr(u, "total_tokens", 0))):
                usage[key] = usage.get(key, 0) + (val or 0)
        return resp.choices[0].message.content or ""

    async def analyze(self, digest: dict, rule_checks: frozenset[str] = frozenset(),
                      nav_labels: frozenset[str] = frozenset(), usage: Optional[dict] = None) -> LLMOutput:
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(digest, ensure_ascii=False)}]
        raw = await self._chat(messages, usage)
        today = date.fromisoformat(digest["context"]["analysis_date"])
        elsewhere = served_elsewhere(digest["site"], digest["context"].get("related_domains") or [])

        def parse(text: str) -> LLMOutput:
            out = LLMOutput.model_validate(_extract_json(text))
            problems = output_problems(out)
            if problems:
                raise ValueError("; ".join(problems))
            return out

        # Up to two correction rounds. Unparseable JSON keeps retrying; after one round, content-level
        # problems are tolerated because clean_output strips junk keywords anyway.
        out = None
        for attempt in range(3):
            try:
                out = parse(raw)
                break
            except (ValueError, ValidationError) as exc:
                broken_json = isinstance(exc, json.JSONDecodeError) or "no JSON object" in str(exc)
                if attempt >= 1 and not broken_json:
                    out = LLMOutput.model_validate(_extract_json(raw))
                    log.warning("LLM output still has problems after correction, keeping the cleanable parts: %s",
                                str(exc)[:200])
                    break
                if attempt == 2:
                    raise
                log.warning("invalid LLM output, asking for a correction: %s", str(exc)[:200])
                hint = (" The JSON did not parse: escape or avoid double quotes inside string values (use single "
                        "quotes for quoted text) and return exactly one object." if broken_json else "")
                messages += [{"role": "assistant", "content": raw[-6000:]},
                             {"role": "user", "content": f"That response was invalid: {str(exc)[:800]}.{hint}\n"
                                                         "Return ONLY the corrected JSON object, prose in English."}]
                raw = await self._chat(messages, usage)
        home = next((p for p in digest.get("pages", []) if p.get("type") == "home"), {})
        return clean_output(out, today, rule_checks, nav_labels, elsewhere, home.get("existing_paths") or [])
