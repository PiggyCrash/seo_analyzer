"""Polite async crawler: robots.txt, sitemap discovery, key-page selection, blocker handling."""
from __future__ import annotations

import asyncio
import gzip
import logging
import re
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .blockers import detect_blocker, has_hard_block_signal, page_flags
from .config import CrawlConfig
from .schema import (BlockerCount, BrokenURL, CrawlSummary, DuplicateSlug, HttpsInfo, NotFoundProbe, PageFetch,
                     RobotsInfo, SitemapInfo, TagManagerInfo)
from .urlutils import (bare_host, classify_path, in_scope, is_crawlable, locale_prefix, normalize_input,
                       normalize_url, origin_candidates, origin_of, site_key, swap_locale_prefix)

log = logging.getLogger(__name__)

REDIRECT_CODES = {301, 302, 303, 307, 308}
RETRYABLE = {429, 500, 502, 503, 504}
NETWORK_ERRORS = {"dns_error", "ssl_error", "connect_error", "timeout"}
DEAD_CODES = {404, 410}


@dataclass
class _Resp:
    status: int
    headers: dict
    body: bytes
    content_type: str
    truncated: bool = False


@dataclass
class FetchResult:
    url: str
    final_url: Optional[str] = None
    status_code: Optional[int] = None
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    text: str = ""
    content_type: str = ""
    size: int = 0
    redirects: list = field(default_factory=list)
    elapsed_ms: int = 0
    outcome: str = "ok"
    blocker: Optional[str] = None
    error: Optional[str] = None
    flags: list = field(default_factory=list)
    source: Optional[str] = None
    soup: Any = field(default=None, repr=False)

    def to_model(self) -> PageFetch:
        return PageFetch(url=self.url, final_url=self.final_url, status_code=self.status_code,
                         outcome=self.outcome, blocker=self.blocker, error=self.error,
                         content_type=self.content_type or None, bytes=self.size,
                         elapsed_ms=self.elapsed_ms, redirects=self.redirects, flags=self.flags,
                         source=self.source)

    def to_cache(self) -> dict:
        return {**self.to_model().model_dump(), "headers": self.headers}

    @classmethod
    def from_cache(cls, d: dict, text: str) -> "FetchResult":
        return cls(url=d["url"], final_url=d.get("final_url"), status_code=d.get("status_code"),
                   headers=d.get("headers") or {}, text=text, content_type=d.get("content_type") or "",
                   size=d.get("bytes", 0), redirects=d.get("redirects") or [],
                   elapsed_ms=d.get("elapsed_ms", 0), outcome=d["outcome"], blocker=d.get("blocker"),
                   error=d.get("error"), flags=d.get("flags") or [], source=d.get("source"))


@dataclass
class CrawlResult:
    input_url: str
    user_agent: str
    start_url: Optional[str] = None
    final_url: Optional[str] = None
    final_domain: Optional[str] = None
    status: Optional[str] = None
    reason: Optional[str] = None
    pages: list = field(default_factory=list)
    robots: RobotsInfo = field(default_factory=RobotsInfo)
    sitemap: SitemapInfo = field(default_factory=SitemapInfo)
    https: Optional[HttpsInfo] = None
    not_found_probes: list = field(default_factory=list)
    duplicate_slugs: list = field(default_factory=list)
    tag_manager: Optional[TagManagerInfo] = None
    budget_exceeded: Optional[str] = None
    stopped_early: Optional[str] = None
    duration_seconds: float = 0.0

    @property
    def site_key(self) -> str:
        return site_key(self.final_url or self.start_url or self.input_url)

    def finalize(self, t0: float) -> "CrawlResult":
        self.duration_seconds = round(time.monotonic() - t0, 2)
        if self.status is None:
            ok = sum(p.outcome == "ok" for p in self.pages)
            failed = sum(p.outcome == "failed" for p in self.pages)
            blocked = any(p.outcome == "blocked" for p in self.pages)
            reasons = []
            if self.stopped_early:
                reasons.append(f"stopped_early:{self.stopped_early}")
            if self.budget_exceeded:
                reasons.append(f"budget_exceeded:{self.budget_exceeded}")
            if blocked:
                reasons.append("some_pages_blocked")
            if failed > ok:
                reasons.append("many_pages_failed")
            self.status = "partial" if reasons else "ok"
            self.reason = ", ".join(reasons) or None
        return self

    def summary(self) -> CrawlSummary:
        counted = [p for p in self.pages if p.outcome != "skipped"]
        blockers = Counter(p.blocker for p in self.pages if p.outcome == "blocked" and p.blocker)
        flags = sorted({f for p in self.pages for f in p.flags})
        return CrawlSummary(
            status=self.status or "failed", reason=self.reason, input_url=self.input_url,
            start_url=self.start_url, final_url=self.final_url, final_domain=self.final_domain,
            pages_attempted=len(counted),
            pages_ok=sum(p.outcome == "ok" for p in self.pages),
            pages_blocked=sum(p.outcome == "blocked" for p in self.pages),
            pages_failed=sum(p.outcome == "failed" for p in self.pages),
            pages_skipped=sum(p.outcome == "skipped" for p in self.pages),
            blockers=[BlockerCount(type=t, count=c) for t, c in blockers.most_common()],
            broken_urls=[BrokenURL(url=p.url, status_code=p.status_code, source=p.source)
                         for p in self.pages if p.outcome == "failed" and p.status_code in DEAD_CODES],
            sitemap_urls_fetched=sum(p.source in ("sitemap", "both") and p.outcome != "skipped"
                                     for p in self.pages),
            flags=flags, budget_exceeded=self.budget_exceeded, stopped_early=self.stopped_early,
            robots=self.robots, sitemap=self.sitemap, https=self.https,
            not_found_probes=self.not_found_probes, duplicate_slugs=self.duplicate_slugs,
            tag_manager=self.tag_manager,
            duration_seconds=self.duration_seconds, user_agent=self.user_agent)

    def to_cache(self) -> dict:
        return {"input_url": self.input_url, "user_agent": self.user_agent,
                "start_url": self.start_url, "final_url": self.final_url,
                "final_domain": self.final_domain, "status": self.status, "reason": self.reason,
                "robots": self.robots.model_dump(), "sitemap": self.sitemap.model_dump(),
                "https": self.https.model_dump() if self.https else None,
                "not_found_probes": [p.model_dump() for p in self.not_found_probes],
                "duplicate_slugs": [d.model_dump() for d in self.duplicate_slugs],
                "tag_manager": self.tag_manager.model_dump() if self.tag_manager else None,
                "budget_exceeded": self.budget_exceeded, "stopped_early": self.stopped_early,
                "duration_seconds": self.duration_seconds,
                "pages": [p.to_cache() for p in self.pages]}

    @classmethod
    def from_cache(cls, d: dict, texts: list[str]) -> "CrawlResult":
        r = cls(input_url=d["input_url"], user_agent=d.get("user_agent", ""),
                start_url=d.get("start_url"), final_url=d.get("final_url"),
                final_domain=d.get("final_domain"), status=d.get("status"), reason=d.get("reason"),
                robots=RobotsInfo(**(d.get("robots") or {})),
                sitemap=SitemapInfo(**(d.get("sitemap") or {})),
                https=HttpsInfo(**d["https"]) if d.get("https") else None,
                not_found_probes=[NotFoundProbe(**p) for p in d.get("not_found_probes") or []],
                duplicate_slugs=[DuplicateSlug(**x) for x in d.get("duplicate_slugs") or []],
                tag_manager=TagManagerInfo(**d["tag_manager"]) if d.get("tag_manager") else None,
                budget_exceeded=d.get("budget_exceeded"), stopped_early=d.get("stopped_early"),
                duration_seconds=d.get("duration_seconds", 0.0))
        r.pages = [FetchResult.from_cache(p, t) for p, t in zip(d.get("pages", []), texts)]
        return r


class Throttle:
    """Per-site politeness: max N in flight, and at least `delay` seconds between request starts."""

    def __init__(self, concurrency: int, delay: float):
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.delay = delay
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def __aenter__(self):
        await self.sem.acquire()
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.delay

    async def __aexit__(self, *exc):
        self.sem.release()


def _classify_exc(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    msg = f"{exc!r} {exc}".lower()
    if "certificate" in msg or "ssl" in msg or "tls" in msg:
        return "ssl_error"
    if any(s in msg for s in ("getaddrinfo", "name or service not known", "nodename nor servname",
                              "no address associated", "name resolution", "temporary failure in name")):
        return "dns_error"
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError,
                        httpx.WriteError, httpx.ProxyError, ConnectionError)):
        return "connect_error"
    if isinstance(exc, httpx.InvalidURL):
        return "invalid_url"
    return "http_error"


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w.-]+)", content_type or "")
    enc = m.group(1) if m else None
    if not enc:
        m2 = re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", body[:4096], re.I)
        enc = m2.group(1).decode("ascii", "ignore") if m2 else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _visible_words(soup: BeautifulSoup) -> int:
    for t in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
        t.decompose()
    root = soup.body or soup
    return len(root.get_text(" ", strip=True).split())


def parse_sitemap(body: bytes) -> Optional[tuple[bool, list[str]]]:
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            return None
    head = body[:5000].lower()
    if b"<urlset" not in head and b"<sitemapindex" not in head:
        return None
    soup = BeautifulSoup(body, "xml")
    if soup.find("sitemapindex"):
        return True, [l.get_text(strip=True) for l in soup.find_all("loc") if l.parent.name == "sitemap"]
    if soup.find("urlset"):
        return False, [l.get_text(strip=True) for l in soup.find_all("loc") if l.parent.name == "url"]
    return None


def select_pages(home: FetchResult, sitemap_urls: list[str], base_host: str,
                 cfg: CrawlConfig) -> list[tuple[str, str]]:
    """Rank candidate URLs, best first, as (url, source) with source = homepage_link | sitemap | both.

    Returns more than max_pages candidates so dead URLs can be replaced during the crawl.
    """
    home_norm = normalize_url(home.final_url)
    stats: dict[str, dict] = {}

    def ok(u: Optional[str]) -> bool:
        return bool(u) and u != home_norm and in_scope(u, base_host, cfg.include_subdomains) \
            and is_crawlable(u) and not urlparse(u).query

    for a in home.soup.find_all("a", href=True) if home.soup else []:
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#", "data:", "sms:")):
            continue
        u = normalize_url(href, base=home.final_url)
        if not ok(u):
            continue
        s = stats.setdefault(u, {"count": 0, "nav": False, "anchor": ""})
        s["count"] += 1
        if a.find_parent(["nav", "header"]):
            s["nav"] = True
        if not s["anchor"]:
            s["anchor"] = a.get_text(" ", strip=True)[:80]
    in_sitemap = set()
    for raw in sitemap_urls:
        u = normalize_url(raw)
        if ok(u):
            stats.setdefault(u, {"count": 0, "nav": False, "anchor": ""})
            in_sitemap.add(u)

    scored = []
    for u, s in stats.items():
        path = urlparse(u).path
        ptype, base = classify_path(path)
        if ptype == "other" and s["anchor"]:
            t2, b2 = classify_path("/" + re.sub(r"\s+", "-", s["anchor"].lower()))
            if t2 not in ("other", "home", "blog_post", "blog_index"):
                ptype, base = t2, b2 - 1
        depth = path.strip("/").count("/") + 1
        score = base + min(s["count"], 3) + (2 if s["nav"] else 0) - 0.5 * depth
        scored.append((score, ptype, u))
    scored.sort(key=lambda x: (-x[0], x[2]))

    slots = max(0, cfg.max_pages - 1)
    max_posts = max(2, int(slots * 0.4))
    min_posts = min(2, max_posts)
    chosen, posts, legal = [], 0, 0
    for _, ptype, u in scored:
        if ptype == "blog_post":
            if posts >= max_posts:
                continue
            posts += 1
        if ptype == "legal":
            if legal >= 1:
                continue
            legal += 1
        linked = stats[u]["count"] > 0
        chosen.append((u, ptype, "both" if linked and u in in_sitemap else ("homepage_link" if linked else "sitemap")))
    reserved = [c for c in chosen if c[1] == "blog_post"][:min_posts]
    rest = [c for c in chosen if c not in reserved]
    head = max(0, slots - len(reserved))
    ordered = rest[:head] + reserved + rest[head:]
    return [(u, src) for u, _, src in ordered[:max(slots, cfg.max_fetch_attempts)]]


def probe_sample(urls: list[str], exclude: set[str], n: int) -> list[str]:
    """Up to n URLs spread evenly across the sitemap, skipping ones already fetched.

    Every large locale prefix (/id/, /en-GB/...) gets a slot first, so a dead prefix is not missed."""
    pool = [u for u in dict.fromkeys(urls) if u not in exclude]
    if n <= 0 or not pool:
        return []
    groups: dict[Optional[str], list[str]] = {}
    for u in pool:
        groups.setdefault(locale_prefix(u), []).append(u)
    picked = [g[len(g) // 2] for k, g in sorted(groups.items(), key=lambda kv: -len(kv[1]))
              if k is not None and len(g) >= 5][:max(1, n // 2)]
    rest = [u for u in pool if u not in picked]
    step = max(1, len(rest) // max(1, n - len(picked)))
    return (picked + rest[::step])[:n]


# Page-builder template post types that should never be public (Royal Addons, Elementor, Divi, Astra, ...).
TEMPLATE_SITEMAP = re.compile(r"(wpr_templates|elementor_library|elementor-hf|et_pb_layout|astra-advanced-hook"
                              r"|jet-theme-core|ct_template|fl-builder-template|oceanwp_library|elementskit_template"
                              r"|ae_global_templates|jkit-template)[-_]?sitemap", re.I)
SITEMAP_GOOD = re.compile(r"cms|page|post|blog|news|main|article|sitemap(_index)?\.xml$", re.I)
SITEMAP_NOISE = re.compile(r"product|download|document|faq|image|video|google-|category|tag|author|feed", re.I)


def rank_sitemaps(urls: list[str], home_url: Optional[str]) -> list[str]:
    """Best sitemaps first: same path prefix as the homepage (/sg/en/ on a multi-country site), then
    content sitemaps (cms, pages, posts) before product/download/image feeds."""
    home_segs = [s for s in urlparse(home_url or "").path.split("/") if s][:2]

    def key(u: str):
        segs = [s for s in urlparse(u).path.split("/") if s]
        shared = 0
        for a, b in zip(home_segs, segs):
            if a.lower() != b.lower():
                break
            shared += 1
        return (-shared, bool(SITEMAP_NOISE.search(u)), not SITEMAP_GOOD.search(u), len(u), u)
    return sorted(dict.fromkeys(urls), key=key)


def _count_hreflang(body: bytes) -> int:
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            return 0
    return body.count(b"hreflang=")


_Q = r"\\*[\"']"  # a quote, possibly JS-escaped inside the container's JSON
GTM_PATTERNS = {
    "ga4": re.compile(r"\b(G-[A-Z0-9]{6,12})\b"),
    "google_ads": re.compile(r"\b(AW-\d{6,12})\b"),
    "universal_analytics": re.compile(r"\b(UA-\d{4,10}-\d{1,4})\b"),
    "meta_pixel": re.compile(rf"fbq\({_Q}init{_Q}\s*,\s*{_Q}(\d{{10,20}}){_Q}|{_Q}vtp_pixelIds?{_Q}\s*[:,]\s*{_Q}(\d{{10,20}})"),
    "linkedin_insight": re.compile(rf"_linkedin_partner_id\s*=\s*{_Q}?(\d{{4,10}})|{_Q}function{_Q}:{_Q}__bzi{_Q}[^}}]*?"
                                   rf"{_Q}vtp_id{_Q}:{_Q}(\d{{4,10}})"),
    "tiktok_pixel": re.compile(rf"ttq\.load\({_Q}([A-Z0-9]{{15,30}}){_Q}"),
    "hotjar": re.compile(r"hjid\s*:\s*(\d{5,9})"),
    "microsoft_clarity": re.compile(rf"clarity{_Q}\s*,\s*{_Q}script{_Q}\s*,\s*{_Q}([a-z0-9]{{8,12}}){_Q}"),
}
DUPLICATE_SLUG = re.compile(r"^(?P<base>/.+?)-(?P<n>\d{1,2})/?$")


def duplicate_slug_pairs(home: FetchResult) -> list[tuple[str, str]]:
    """Homepage links like /projects/woodfibre-lng and /projects/woodfibre-lng-0 (CMS duplicate aliases)."""
    host = bare_host(urlparse(home.final_url or home.url).hostname)
    paths = set()
    for a in home.soup.find_all("a", href=True) if home.soup else []:
        u = normalize_url(a["href"], base=home.final_url)
        if u and bare_host(urlparse(u).hostname) == host:
            paths.add(urlparse(u).path.rstrip("/") or "/")
    pairs = []
    for p in sorted(paths):
        m = DUPLICATE_SLUG.match(p)
        # "-0".."-9" suffixes are CMS counters (Drupal, WordPress); the base URL is checked even when unlinked.
        if m and len(m.group("n")) == 1 and not re.search(r"\d{2,}$", m.group("base")):
            pairs.append((m.group("base"), p))
    pairs.sort(key=lambda ab: ab[0] not in paths)
    return pairs


def _home_link_prefixes(home: FetchResult) -> list[str]:
    """Locale prefixes of the homepage's same-site links (one entry per link, www and non-www alike)."""
    host = bare_host(urlparse(home.final_url or home.url).hostname)
    out = []
    for a in home.soup.find_all("a", href=True) if home.soup else []:
        u = normalize_url(a["href"], base=home.final_url)
        if u and bare_host(urlparse(u).hostname) == host and locale_prefix(u):
            out.append(locale_prefix(u))
    return out


def sitemap_hygiene(raw_locs: list[str]) -> dict:
    """Duplicates, malformed entries and locale-prefix distribution of the raw <loc> values."""
    malformed = []
    for u in raw_locs:
        p = urlparse(u)
        if not p.scheme or not p.hostname or "//" in p.path or re.search(r"https?:", p.path) or " " in u:
            malformed.append(u)
    prefixes = Counter(locale_prefix(u) or "(none)" for u in raw_locs)
    truncated = [u for u in dict.fromkeys(raw_locs) if urlparse(u).path.rstrip("/").endswith("-")]
    return {"unique_urls": len(set(raw_locs)), "duplicate_entries": len(raw_locs) - len(set(raw_locs)),
            "malformed_entries": len(malformed), "malformed_examples": list(dict.fromkeys(malformed))[:5],
            "prefix_counts": dict(prefixes.most_common(8)),
            "truncated_slugs": len(truncated), "truncated_examples": truncated[:3]}


class _SiteSession:
    def __init__(self, cfg: CrawlConfig, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client
        self.throttle = Throttle(cfg.per_domain_concurrency, cfg.delay_seconds)
        self.rp: Optional[robotparser.RobotFileParser] = None
        self.bytes_total = 0

    def _allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots or self.rp is None:
            return True
        try:
            return self.rp.can_fetch(self.cfg.user_agent, url)
        except Exception:
            return True

    def _backoff(self, attempt: int) -> float:
        return min(1.5 * (2 ** attempt), 20.0)

    def _retry_after(self, r: _Resp, attempt: int) -> float:
        ra = r.headers.get("retry-after", "").strip()
        return min(float(ra), 30.0) if ra.isdigit() else self._backoff(attempt)

    async def _get_once(self, url: str, html_only: bool) -> _Resp:
        async with self.throttle:
            async with self.client.stream("GET", url) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                ctype = headers.get("content-type", "").lower()
                status = resp.status_code
                if status in REDIRECT_CODES:
                    return _Resp(status, headers, b"", ctype)
                if html_only and status < 400 and ctype and "html" not in ctype:
                    return _Resp(status, headers, b"", ctype)
                chunks, size, truncated = [], 0, False
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > self.cfg.max_page_bytes:
                        truncated = True
                        break
                    chunks.append(chunk)
                return _Resp(status, headers, b"".join(chunks), ctype, truncated)

    async def _request(self, url: str, html_only: bool = True) -> tuple[Optional[_Resp], Optional[str]]:
        attempt = 0
        while True:
            try:
                r = await self._get_once(url, html_only)
            except Exception as exc:
                err = _classify_exc(exc)
                if err not in ("timeout", "connect_error") or attempt >= self.cfg.max_retries:
                    log.debug("request failed %s: %s (%s)", url, err, exc)
                    return None, err
                await asyncio.sleep(self._backoff(attempt))
                attempt += 1
                continue
            if r.status in RETRYABLE and attempt < self.cfg.max_retries:
                if not has_hard_block_signal(r.headers, r.body[:30000].decode("latin-1").lower()):
                    await asyncio.sleep(self._retry_after(r, attempt))
                    attempt += 1
                    continue
            return r, None

    async def fetch(self, url: str, kind: str = "html") -> FetchResult:
        """kind='html' for pages (parsed + classified), 'raw' for robots.txt / sitemaps."""
        res = FetchResult(url=url)
        t0 = time.monotonic()
        current, seen = url, {url}
        r: Optional[_Resp] = None
        for _ in range(self.cfg.max_redirects + 1):
            r, err = await self._request(current, html_only=(kind == "html"))
            if err:
                res.outcome, res.error = "failed", err
                break
            loc = r.headers.get("location")
            if r.status in REDIRECT_CODES and loc:
                nxt = urljoin(current, loc)
                res.redirects.append(current)
                if nxt in seen:
                    res.outcome, res.error = "failed", "redirect_loop"
                    break
                seen.add(nxt)
                current = nxt
                continue
            break
        else:
            res.outcome, res.error = "failed", "too_many_redirects"
        res.elapsed_ms = int((time.monotonic() - t0) * 1000)
        if res.outcome == "failed" or r is None:
            return res

        res.final_url, res.status_code, res.headers = current, r.status, r.headers
        res.content_type, res.body, res.size = r.content_type, r.body, len(r.body)
        self.bytes_total += len(r.body)
        if r.truncated:
            res.flags.append("truncated")

        if kind == "raw":
            res.text = _decode(r.body, r.content_type)
            if r.status >= 400:
                b = detect_blocker(r.status, r.headers, res.text.lower()[:50000], 0, url, current)
                res.outcome = "blocked" if b else "failed"
                res.blocker, res.error = b, (None if b else f"http_{r.status}")
            return res

        if r.status < 400 and r.content_type and "html" not in r.content_type:
            res.outcome, res.error, res.body = "skipped", "non_html", b""
            return res
        res.text = _decode(r.body, r.content_type) if r.body else ""
        lower = res.text.lower()
        soup = BeautifulSoup(res.text, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        h1 = soup.find("h1")
        h1_text = h1.get_text(" ", strip=True) if h1 else ""
        words = _visible_words(soup)

        blocker = detect_blocker(r.status, r.headers, lower, words, url, current, bool(res.redirects))
        if blocker:
            res.outcome, res.blocker = "blocked", blocker
        elif r.status >= 400:
            res.outcome, res.error = "failed", f"http_{r.status}"
        elif r.status >= 300:
            res.outcome, res.error = "failed", "redirect_without_location"
        else:
            flags = page_flags(lower, words, title, h1_text)
            if "soft_404" in flags:
                res.outcome, res.error = "failed", "soft_404"
            res.flags.extend(flags)
            res.soup = soup
        return res

    async def _fetch_robots(self, origin: str):
        url = origin + "/robots.txt"
        r = await self.fetch(url, kind="raw")
        if r.outcome == "failed" and r.error in NETWORK_ERRORS:
            return None, None, r.error
        info = RobotsInfo(status="missing", url=url, http_status=r.status_code)
        if r.outcome == "blocked":
            info.status = "blocked"
            return info, None, None
        if r.status_code == 200 and r.text and "<html" not in r.text[:500].lower():
            rp = robotparser.RobotFileParser()
            rp.parse(r.text.splitlines())
            home = origin + "/"
            info.status = "present"
            info.disallows_all = not rp.can_fetch("*", home)
            info.blocks_googlebot = not rp.can_fetch("Googlebot", home)
            info.blocks_this_crawler = not rp.can_fetch(self.cfg.user_agent, home)
            delay = rp.crawl_delay(self.cfg.user_agent)
            info.crawl_delay = float(delay) if delay else None
            info.sitemaps = list(rp.site_maps() or [])
            return info, rp, None
        if r.status_code and r.status_code >= 500:
            info.status = "error"
        return info, None, None

    def _apply_robots(self, info: Optional[RobotsInfo], rp) -> None:
        self.rp = rp
        if info and info.crawl_delay:
            self.throttle.delay = max(self.cfg.delay_seconds, min(info.crawl_delay, 10.0))

    async def _https_info(self, home: FetchResult, ssl_error: bool) -> HttpsInfo:
        p = urlparse(home.final_url)
        info = HttpsInfo(final_scheme=p.scheme, ssl_error=ssl_error,
                         hsts="strict-transport-security" in home.headers)
        if p.scheme == "https":
            r, err = await self._request(f"http://{p.netloc}/", html_only=True)
            if r is not None and not err:
                loc = r.headers.get("location", "")
                info.http_redirects_to_https = r.status in REDIRECT_CODES and \
                    urljoin(f"http://{p.netloc}/", loc).startswith("https://")
        else:
            info.http_redirects_to_https = False
        return info

    async def _discover_sitemap(self, origin: str, declared: list[str],
                                home_url: Optional[str] = None) -> tuple[SitemapInfo, list[str]]:
        tries = rank_sitemaps(declared, home_url)[:3] or [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
        invalid_url = None
        for url in tries:
            if not self._allowed(url):
                continue
            r = await self.fetch(url, kind="raw")
            if r.outcome == "blocked":
                return SitemapInfo(status="blocked", url=url, declared_in_robots=bool(declared)), []
            if r.status_code != 200 or not r.body:
                continue
            parsed = parse_sitemap(r.body)
            if parsed is None:
                invalid_url = invalid_url or url
                continue
            is_index, locs = parsed
            urls, children = locs, None
            hreflang = _count_hreflang(r.body)
            obsolete = b"schemas/sitemap/0.84" in r.body[:3000]
            dead_children = []
            template_maps = [u for u in locs if TEMPLATE_SITEMAP.search(u)] if is_index else []
            if is_index:
                children = len(locs)
                urls = []
                ranked = rank_sitemaps([u for u in locs if u not in template_maps], home_url) + template_maps
                for child in ranked[:3]:
                    cr = await self.fetch(child, kind="raw")
                    if cr.status_code == 200 and cr.body:
                        cp = parse_sitemap(cr.body)
                        if cp and not cp[0]:
                            urls.extend(cp[1])
                            hreflang += _count_hreflang(cr.body)
                            obsolete = obsolete or b"schemas/sitemap/0.84" in cr.body[:3000]
                    else:
                        dead_children.append(child)
                # Children not read for URLs still get a status check: a listed child that 404s is a real bug.
                for child in ranked[3:12]:
                    cr = await self.fetch(child, kind="raw")
                    if cr.status_code in DEAD_CODES:
                        dead_children.append(child)
            blog = sum(classify_path(urlparse(u).path)[0] == "blog_post" for u in urls)
            return SitemapInfo(status="present", url=url, url_count=len(urls), blog_url_count=blog,
                               is_index=is_index, child_sitemaps=children,
                               declared_in_robots=bool(declared), declared_count=len(declared),
                               hreflang_annotations=hreflang, obsolete_namespace=obsolete,
                               dead_children=dead_children[:5], template_sitemaps=template_maps[:5],
                               homepage_listed=any(normalize_url(u) == normalize_url(home_url) for u in urls)
                               if home_url else None,
                               **sitemap_hygiene(urls)), urls[:2000]
        return SitemapInfo(status="invalid" if invalid_url else "missing", url=invalid_url,
                           declared_in_robots=bool(declared)), []

    async def _crawl_pages(self, urls: list[tuple[str, str]], result: CrawlResult, base_host: str,
                           t0: float) -> None:
        """Fetch candidates in rank order until max_pages usable pages or max_fetch_attempts fetches."""
        queue = list(urls)
        seen_final = {normalize_url(result.pages[0].final_url)}
        state = {"stop": None, "rate_limited": 0, "ok": 0, "inflight": 0, "fetched": 0}
        target = max(0, self.cfg.max_pages - 1)

        async def worker():
            while (queue and not state["stop"] and state["ok"] + state["inflight"] < target
                   and state["fetched"] < self.cfg.max_fetch_attempts):
                if time.monotonic() - t0 > self.cfg.site_time_budget:
                    result.budget_exceeded = "time"
                    return
                if self.bytes_total > self.cfg.max_site_bytes:
                    result.budget_exceeded = "bytes"
                    return
                url, source = queue.pop(0)
                if not self._allowed(url):
                    result.pages.append(FetchResult(url=url, outcome="skipped", error="robots_disallowed",
                                                    source=source))
                    continue
                state["inflight"] += 1
                state["fetched"] += 1
                try:
                    r = await self.fetch(url)
                finally:
                    state["inflight"] -= 1
                r.soup, r.source = None, source
                if r.outcome == "ok" and r.final_url:
                    fn = normalize_url(r.final_url)
                    if not in_scope(r.final_url, base_host, self.cfg.include_subdomains):
                        r.outcome, r.error, r.text, r.body = "skipped", "redirected_off_site", "", b""
                    elif fn in seen_final:
                        r.outcome, r.error, r.text, r.body = "skipped", "duplicate", "", b""
                    else:
                        seen_final.add(fn)
                        state["ok"] += 1
                result.pages.append(r)
                if r.blocker in ("anti_bot", "captcha"):
                    state["stop"] = r.blocker
                elif r.blocker == "rate_limited":
                    state["rate_limited"] += 1
                    if state["rate_limited"] >= 2:
                        state["stop"] = "rate_limited"

        await asyncio.gather(*(worker() for _ in range(max(1, self.cfg.per_domain_concurrency))))
        result.stopped_early = state["stop"]

    async def run(self, input_url: str) -> CrawlResult:
        t0 = time.monotonic()
        result = CrawlResult(input_url=input_url, user_agent=self.cfg.user_agent)
        start = normalize_input(input_url)
        result.start_url = start

        origin, last_err, https_ssl_error = None, None, False
        robots_info, rp = None, None
        for cand in origin_candidates(start):
            robots_info, rp, err = await self._fetch_robots(cand)
            if err:
                last_err = err
                if cand.startswith("https://") and err == "ssl_error":
                    https_ssl_error = True
                log.info("origin %s unreachable (%s)", cand, err)
                continue
            origin = cand
            break
        if origin is None:
            result.status, result.reason = "failed", last_err or "unreachable"
            return result.finalize(t0)
        result.robots = robots_info
        self._apply_robots(robots_info, rp)

        home_url = origin + "/"
        if not self._allowed(home_url):
            result.status, result.reason = "blocked", "robots_disallowed"
            return result.finalize(t0)

        home = await self.fetch(home_url)
        result.pages.append(home)
        if home.outcome != "ok":
            result.status = "blocked" if home.outcome == "blocked" else "failed"
            result.reason = f"homepage_{home.blocker or home.error or home.outcome}"
            return result.finalize(t0)

        final_origin = origin_of(home.final_url)
        if final_origin != origin:
            info2, rp2, err2 = await self._fetch_robots(final_origin)
            if not err2:
                result.robots = info2
                self._apply_robots(info2, rp2)
        base_host = urlparse(home.final_url).hostname
        result.final_url = home.final_url
        result.final_domain = bare_host(base_host)

        result.https = await self._https_info(home, https_ssl_error)
        result.not_found_probes = await self._probe_not_found(final_origin, home)
        result.duplicate_slugs = await self._check_duplicate_slugs(final_origin, home)
        result.tag_manager = await self._read_tag_manager(home)
        result.sitemap, sitemap_urls = await self._discover_sitemap(final_origin, result.robots.sitemaps,
                                                                    home.final_url)

        candidates = select_pages(home, sitemap_urls, base_host, self.cfg)
        log.info("%s: %d candidate pages selected", result.final_domain, len(candidates))
        await self._crawl_pages(candidates, result, base_host, t0)
        if not result.stopped_early:
            await self._probe_sitemap(sitemap_urls, result, base_host, t0)
        return result.finalize(t0)

    async def _probe_not_found(self, origin: str, home: FetchResult) -> list[NotFoundProbe]:
        """Request URLs that cannot exist (at the root and under the main locale prefix).
        A 200 answer means the CMS serves soft-404s: every junk URL becomes an indexable empty page."""
        prefixes: Counter = Counter()
        for lp in _home_link_prefixes(home):
            prefixes[lp] += 1
        token = "seo-probe-" + secrets.token_hex(4)
        paths = [f"/{token}"] + ([f"/{prefixes.most_common(1)[0][0]}/{token}"] if prefixes else [])
        out = []
        for path in paths:
            url = origin + path
            if not self._allowed(url):
                continue
            r = await self.fetch(url, kind="raw")
            if r.outcome == "blocked" or r.status_code is None:
                continue
            soup = BeautifulSoup(r.text or "", "lxml")
            title = soup.title.get_text(" ", strip=True) if soup.title else None
            out.append(NotFoundProbe(url=url, status_code=r.status_code, title=title, words=_visible_words(soup),
                                     final_url=r.final_url if r.final_url != url else None))
        return out

    async def _read_tag_manager(self, home: FetchResult) -> Optional[TagManagerInfo]:
        """Most tracking lives inside the GTM container, not the page. Read the public container once."""
        m = re.search(r"\bGTM-[A-Z0-9]{4,9}\b", home.text or "")
        if not m:
            return None
        info = TagManagerInfo(container=m.group(0))
        r = await self.fetch(f"https://www.googletagmanager.com/gtm.js?id={m.group(0)}", kind="raw")
        if r.status_code != 200 or not r.text:
            return info
        info.fetched = True
        js = r.text
        for name, rx in GTM_PATTERNS.items():
            ids = sorted({x if isinstance(x, str) else next((g for g in x if g), "") for x in rx.findall(js)} - {""})
            if ids:
                info.ids[name] = ids[:5]
        info.ua_references = len(re.findall(r"UA-\d{4,10}-\d{1,4}", js))
        return info

    async def _check_duplicate_slugs(self, origin: str, home: FetchResult) -> list[DuplicateSlug]:
        """Fetch both halves of up to 3 duplicate-alias pairs and compare status, title and canonical."""
        out = []
        for a, b in duplicate_slug_pairs(home)[:4]:
            info = {}
            for key, path in (("a", a), ("b", b)):
                url = origin + path
                if not self._allowed(url):
                    break
                r = await self.fetch(url, kind="raw")
                soup = BeautifulSoup(r.text or "", "lxml")
                canon = soup.find("link", rel="canonical")
                info[key] = (r.status_code, soup.title.get_text(" ", strip=True) if soup.title else None,
                             canon.get("href") if canon else None)
            if len(info) == 2:
                out.append(DuplicateSlug(path_a=a, path_b=b, status_a=info["a"][0], status_b=info["b"][0],
                                         title_a=info["a"][1], title_b=info["b"][1],
                                         canonical_a=info["a"][2], canonical_b=info["b"][2]))
        return out

    async def _diagnose_prefixes(self, result: CrawlResult, t0: float) -> None:
        """Dead sitemap URLs under a locale prefix the site does not serve (/en-GB/ vs /en/): retry the
        same slug under each prefix that does work, and record the prefix that fixes it."""
        sm = result.sitemap
        dead = sm.probe_dead_examples + [p.url for p in result.pages
                                         if p.outcome == "failed" and p.status_code in DEAD_CODES]
        live = Counter(locale_prefix(p.final_url or p.url) for p in result.pages if p.outcome == "ok")
        live.pop(None, None)
        if result.pages:
            # Language versions linked from the homepage (e.g. the /en/ switcher) are candidates too.
            for lp in set(_home_link_prefixes(result.pages[0])):
                live.setdefault(lp, 0)
        by_prefix: dict[str, list[str]] = {}
        for u in dict.fromkeys(dead):
            lp = locale_prefix(u)
            if lp and lp not in live:
                by_prefix.setdefault(lp, []).append(u)
        for dead_prefix, urls in by_prefix.items():
            base = re.split(r"[-_]", dead_prefix)[0].lower()
            for cand in sorted(live, key=lambda x: (x.lower() != base, -live[x]))[:3]:
                if time.monotonic() - t0 > self.cfg.site_time_budget:
                    return
                alt = swap_locale_prefix(urls[0], cand)
                if not self._allowed(alt):
                    continue
                r = await self.fetch(alt, kind="raw")
                if r.status_code == 200:
                    sm.prefix_fixes[dead_prefix] = cand
                    if len(sm.prefix_fix_examples) < 3:
                        sm.prefix_fix_examples.append(f"{urls[0]} -> {alt}")
                    break

    async def _probe_sitemap(self, sitemap_urls: list[str], result: CrawlResult, base_host: str,
                             t0: float) -> None:
        """Status-check a spread-out sample of sitemap URLs to estimate how many are dead. Not analyzed."""
        fetched = {normalize_url(p.url) for p in result.pages}
        # Probe the URLs exactly as the sitemap lists them: normalising (e.g. dropping a trailing slash)
        # would manufacture redirects that the site never serves to search engines.
        in_site = [x for x in dict.fromkeys(sitemap_urls)
                   if normalize_url(x) and normalize_url(x) not in fetched
                   and in_scope(x, base_host, self.cfg.include_subdomains)]
        sm = result.sitemap
        for url in probe_sample(in_site, fetched, self.cfg.sitemap_probe):
            if time.monotonic() - t0 > self.cfg.site_time_budget or not self._allowed(url):
                break
            r = await self.fetch(url, kind="raw")
            if r.outcome == "blocked":
                break
            sm.probe_checked += 1
            if r.redirects and r.status_code == 200:
                sm.probe_redirected += 1
                if len(sm.probe_redirect_examples) < 3:
                    sm.probe_redirect_examples.append(f"{url} -> {r.final_url}")
            if r.status_code in DEAD_CODES:
                sm.probe_dead += 1
                if len(sm.probe_dead_examples) < 5:
                    sm.probe_dead_examples.append(url)
        await self._diagnose_prefixes(result, t0)


class Crawler:
    def __init__(self, cfg: CrawlConfig):
        self.cfg = cfg

    async def crawl_site(self, input_url: str) -> CrawlResult:
        headers = {
            "User-Agent": self.cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = httpx.Timeout(self.cfg.request_timeout, connect=min(10.0, self.cfg.request_timeout))
        limits = httpx.Limits(max_connections=max(4, self.cfg.per_domain_concurrency * 2))
        async with httpx.AsyncClient(headers=headers, timeout=timeout, limits=limits,
                                     follow_redirects=False) as client:
            return await _SiteSession(self.cfg, client).run(input_url)
