"""URL normalization, scoping and page classification."""
from __future__ import annotations

import ipaddress
import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

TRACKING_PARAM = re.compile(
    r"^(utm_.*|gclid|gbraid|wbraid|fbclid|msclkid|yclid|igshid|mc_cid|mc_eid|_hsenc|_hsmi|"
    r"hsa_.*|ref|ref_src|srsltid|_ga|_gl)$", re.I)

SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif", ".ico", ".bmp",
    ".zip", ".rar", ".gz", ".tar", ".7z", ".mp4", ".mov", ".webm", ".mp3", ".wav",
    ".css", ".js", ".json", ".xml", ".txt", ".rss", ".woff", ".woff2", ".ttf", ".eot",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".exe", ".dmg", ".apk",
)

EXCLUDE_PATH = re.compile(
    r"(/login|/log-in|/signin|/sign-in|/signup|/sign-up|/register|/cart|/checkout|"
    r"/my-account|/account(/|$)|/wp-admin|/wp-json|/wp-login|/cdn-cgi/|/feed(/|$)|/rss(/|$)|"
    r"/tag/|/tags/|/category/|/categories/|/author/|/page/\d+|/search(/|$)|/calendar|/events/\d{4})",
    re.I)

_BLOG = r"(blog|news|insights|articles|resources|journal|posts?|stories|learn|read|berita|artikel|siaran-pers|press)"
LOCALE_PREFIX = re.compile(r"^/[a-z]{2}(?:[-_][a-z]{2})?(?=/|$)", re.I)
PAGE_PATTERNS: list[tuple[str, re.Pattern, float]] = [
    ("blog_index", re.compile(rf"^/{_BLOG}(-[a-z0-9-]+)?/?$", re.I), 8),
    ("blog_post", re.compile(rf"^/{_BLOG}/.+", re.I), 4),
    ("about", re.compile(r"(about|who-we-are|our-story|company|mission|tentang|profil|sejarah|visi-dan-misi)", re.I), 10),
    ("pricing", re.compile(r"(pricing|plans|packages)", re.I), 9),
    ("service", re.compile(r"(services?|solutions?|what-we-do|products?|features?|offerings?|capabilit|"
                           r"produk|layanan)", re.I), 9),
    ("case_study", re.compile(r"(case-stud|portfolio|our-work|/work(/|$)|clients|results|success-stor)", re.I), 7),
    ("contact", re.compile(r"(contact|get-in-touch|book-a-call|demo|hubungi|kontak)", re.I), 8),
    ("faq", re.compile(r"(faq|help|support|bantuan)", re.I), 6),
    ("team", re.compile(r"(team|people|careers|jobs|karir|karier)", re.I), 5),
    ("legal", re.compile(r"(privacy|terms|cookie|legal|disclaimer|gdpr|pdpa|imprint|kebijakan-privasi|syarat)", re.I), 1),
]


def _netloc(p) -> str:
    host = (p.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = p.port
    if port and not ((p.scheme == "http" and port == 80) or (p.scheme == "https" and port == 443)):
        return f"{host}:{port}"
    return host


def normalize_input(raw: str) -> str:
    """'www.site.com' -> 'https://www.site.com/'. Always the site root."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty URL")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme.lower() not in ("http", "https") or not p.hostname or " " in raw:
        raise ValueError(f"invalid URL: {raw}")
    try:
        netloc = _netloc(p)
    except ValueError as exc:
        raise ValueError(f"invalid URL: {raw}") from exc
    return f"{p.scheme.lower()}://{netloc}/"


def origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme.lower()}://{_netloc(p)}"


def _is_domain(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost") or "." not in host:
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        return True


def origin_candidates(start_url: str) -> list[str]:
    """Origins to try in order: https, http, then the www/non-www twin."""
    p = urlparse(start_url)
    host = p.hostname.lower()
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    hosts = [host]
    if _is_domain(host):
        hosts.append(host[4:] if host.startswith("www.") else "www." + host)
    first = p.scheme.lower()
    schemes = [first] + [s for s in ("https", "http") if s != first]
    return [f"{s}://{h}{port}" for h in hosts for s in schemes]


def normalize_url(url: str, base: Optional[str] = None) -> Optional[str]:
    try:
        if base:
            url = urljoin(base, url)
        p = urlparse(url)
        if p.scheme.lower() not in ("http", "https") or not p.hostname:
            return None
        netloc = _netloc(p)
    except ValueError:
        return None
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not TRACKING_PARAM.match(k)])
    return urlunparse((p.scheme.lower(), netloc, path, "", query, ""))


def bare_host(host: Optional[str]) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def site_key(url: str) -> str:
    """Stable id for a site: host without www (+ _port if non-standard). Safe as a filename."""
    p = urlparse(url if "://" in url else "https://" + url)
    key = bare_host(p.hostname)
    if p.port and p.port not in (80, 443):
        key += f"_{p.port}"
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


def in_scope(url: str, base_host: str, include_subdomains: bool = False) -> bool:
    host = bare_host(urlparse(url).hostname)
    base = bare_host(base_host)
    return host == base or (include_subdomains and host.endswith("." + base))


def is_crawlable(url: str) -> bool:
    path = urlparse(url).path.lower()
    if path.endswith(SKIP_EXTENSIONS):
        return False
    return not EXCLUDE_PATH.search(path)


def classify_path(path: str) -> tuple[str, float]:
    """Page type from the URL path. A leading locale segment (/id/, /en-GB/) is ignored."""
    if path in ("", "/"):
        return "home", 100.0
    path = LOCALE_PREFIX.sub("", path) or "/"
    if path == "/":
        return "other", 2.0
    for name, rx, score in PAGE_PATTERNS:
        if rx.search(path):
            return name, score
    return "other", 3.0
