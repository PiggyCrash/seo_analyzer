"""Deterministic SEO facts: per-page extraction and site-level rollup. No LLM here."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .locale import GENERIC_LAW, detect_language, primary, site_locale
from .schema import (Check, ContentStats, CrawlSummary, Finding, Locale, PageFacts, PageRecord, TechnicalSEO,
                     not_checked)
from .urlutils import bare_host, classify_path, locale_prefix, normalize_url

TRACKER_IDS = {
    "ga4": re.compile(r"\bG-[A-Z0-9]{6,12}\b"),
    "universal_analytics": re.compile(r"\bUA-\d{4,10}-\d{1,4}\b"),
    "gtm": re.compile(r"\bGTM-[A-Z0-9]{4,9}\b"),
    "google_ads": re.compile(r"\bAW-\d{6,12}\b"),
    "meta_pixel": re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{8,20})['\"]"),
    "tiktok_pixel": re.compile(r"ttq\.load\(\s*['\"]([A-Z0-9]{10,30})['\"]"),
}
TRACKER_MARKERS = {
    "meta_pixel": ("connect.facebook.net", "fbevents.js", "facebook.com/tr?"),
    "tiktok_pixel": ("analytics.tiktok.com",),
    "linkedin_insight": ("snap.licdn.com", "_linkedin_partner_id"),
    "hotjar": ("static.hotjar.com",),
    "microsoft_clarity": ("clarity.ms/tag",),
    "snapchat_pixel": ("sc-static.net/scevent",),
    "pinterest_tag": ("s.pinimg.com/ct/",),
    "x_pixel": ("static.ads-twitter.com",),
    "reddit_pixel": ("redditstatic.com/ads",),
    "plausible": ("plausible.io/js",),
    "segment": ("cdn.segment.com",),
    "hubspot": ("js.hs-scripts.com", "js.hs-analytics.net"),
}
TRACKER_NAMES = {
    "ga4": "Google Analytics 4", "universal_analytics": "Universal Analytics", "gtm": "GTM",
    "meta_pixel": "Meta Pixel", "tiktok_pixel": "TikTok Pixel", "linkedin_insight": "LinkedIn Insight",
    "hotjar": "Hotjar", "microsoft_clarity": "Microsoft Clarity", "plausible": "Plausible",
    "google_ads": "Google Ads", "x_pixel": "X Pixel",
}
CONSENT_VENDORS = (
    ("Cookiebot", ("consent.cookiebot.com", "cookiebot")),
    ("OneTrust", ("cdn.cookielaw.org", "otsdkstub", "onetrust")),
    ("CookieYes", ("cdn-cookieyes.com", "cookieyes", "cookie-law-info")),
    ("Osano", ("cmp.osano.com",)),
    ("Termly", ("app.termly.io",)),
    ("iubenda", ("iubenda.com",)),
    ("Didomi", ("didomi",)),
    ("Usercentrics", ("usercentrics",)),
    ("TrustArc", ("trustarc", "truste.com")),
    ("Quantcast Choice", ("quantcast.mgr.consensu.org", "cmp.quantcast.com")),
    ("Complianz", ("complianz",)),
    ("CookieFirst", ("cookiefirst",)),
    ("Axeptio", ("axeptio",)),
    ("Civic Cookie Control", ("civiccomputing.com",)),
    ("Cookie Script", ("cookie-script.com",)),
    ("Finsweet Cookie Consent", ("fs-cc",)),
    ("Klaro", ("klaro",)),
    ("Cookie Consent (Osano OSS)", ("cookieconsent.min.js", "cookieconsent2")),
)
CONSENT_TEXT = ("we use cookies", "this website uses cookies", "this site uses cookies",
                "cookie settings", "cookie preferences", "accept all cookies", "manage cookies")
PLATFORMS = (
    ("Webflow", ("data-wf-site", "webflow.io", "assets.website-files.com", "website-files.com")),
    ("WordPress", ("/wp-content/", "/wp-includes/")),
    ("Shopify", ("cdn.shopify.com", "shopify.theme")),
    ("Wix", ("static.wixstatic.com", "wix-bolt")),
    ("Squarespace", ("static1.squarespace.com", "squarespace.com")),
    ("Framer", ("framerusercontent.com", "framer.website")),
    ("HubSpot CMS", ("hs-sites.com", "hubspot-cms")),
    ("Ghost", ("ghost-portal", "content=\"ghost")),
    ("Drupal", ("drupal-settings-json", "/sites/default/files/")),
    ("Joomla", ("/media/jui/", "joomla")),
    ("Builder.io (headless CMS)", ("cdn.builder.io", "builder.io%7c", "builder.io|", "builder-component")),
    ("Adobe Experience Manager", ("/etc.clientlibs/", "/content/dam/")),
    ("Sitecore", ("/-/media/", "sitecore")),
    ("Next.js", ("__next_data__", "/_next/static/")),
    ("Nuxt", ("__nuxt__", "/_nuxt/")),
    ("Gatsby", ("___gatsby",)),
)
# Server-side frameworks recognisable from session cookies / headers when the HTML has no CMS markers.
COOKIE_PLATFORMS = (
    ("CodeIgniter (PHP)", ("ci_session=",)),
    ("Laravel (PHP)", ("laravel_session=",)),
    ("Laravel (PHP)", ("xsrf-token=",)),
    ("Django (Python)", ("csrftoken=", "sessionid=")),
    ("ASP.NET", ("asp.net_sessionid=", ".aspxauth=")),
    ("Java (Servlet)", ("jsessionid=",)),
    ("PHP", ("phpsessid=",)),
)
LEGACY_JS = re.compile(r"(html5shiv|respond(\.min)?\.js|oss\.maxcdn\.com|cdn\.rawgit\.com|polyfill\.io)", re.I)
PLACEHOLDER = re.compile(
    r"(lorem ipsum|dolor sit amet|\b[\w.+-]*(?:your|example|email|name|user)[\w.+-]*@(?:your-?domain|domain|example"
     r"|email|company|website|site)\.\w+"
    r"|\b1234567890\b|\b123[-\s.]456[-\s.]7890\b|\+33445677654|\bsample page\b|\bhello world!|\bcoming soon\b)", re.I)
COPYRIGHT_YEAR = re.compile(r"(?:©|\(c\)|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)
ZERO_COUNTER = re.compile(r"(?<![\w.,])~?0\s*(?:[KMB]\+?|\+)(?=\s*[^\W\d_])")
GENERIC_ANCHOR = re.compile(r"^\s*(read more|learn more|more|click here|here|see more|view more|find out more|details"
                            r"|discover more|explore|selengkapnya|baca selengkapnya|lihat selengkapnya|lebih lanjut"
                            r"|more info|continue reading|view details|>>|→)\s*[>»→.]*\s*$", re.I)
PHONE_COUNTRY = (("+65", "SG"), ("+62", "ID"), ("+60", "MY"), ("+66", "TH"), ("+63", "PH"), ("+84", "VN"),
                 ("+61", "AU"), ("+64", "NZ"), ("+44", "GB"), ("+91", "IN"), ("+852", "HK"), ("+81", "JP"),
                 ("+82", "KR"), ("+55", "BR"), ("+49", "EU"), ("+33", "EU"), ("+31", "EU"), ("+1", "US"))
EMAIL_ONLY = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MALFORMED_NUMBER = re.compile(r"\b\d{1,3}(?:,\d{3})*,\d{4,}\b")
EXTENSION_ATTR = re.compile(r"__idm_id__|data-gramm(?:_editor)?|data-new-gr-c-s-check-loaded|data-lt-installed|cz-shortcut-listen")
# Hosts that should never appear in production markup (local dev, staging, platform preview domains).
DEV_HOST = re.compile(r"https?://(?:[\w-]+\.)*(?:localhost|127\.0\.0\.1|[\w-]+\.local|[\w-]+\.lndo\.site|[\w-]+\.test"
                      r"|[\w-]+\.ddev\.site|[\w-]+\.pantheonsite\.io|[\w-]+\.wpengine\.com|[\w-]+\.wpenginepowered\.com"
                      r"|[\w-]+\.vercel\.app|[\w-]+\.netlify\.app|staging\.[\w.-]+|dev\.[\w.-]+)(?::\d+)?(?=[/\"'\s]|$)", re.I)
BARE_TITLE = re.compile(r"[|\-–—:·»]\s*$")
EOL_SOFTWARE = re.compile(r"OpenSSL/1\.[01][\w.-]*|PHP/(?:5\.|7\.[0-4])[\d.]*|Apache/2\.[0-2]\.[\d]+|nginx/1\.(?:[0-9]|1[0-9])\.\d+")
TEMPLATE_SUFFIX = re.compile(r"\s[|\-–—·»]\s[^|\-–—·»]{2,40}$")
GENERIC_HOME_TITLE = re.compile(r"^\s*(home|homepage|home page|beranda|welcome|index|untitled)\b", re.I)
# Visually hidden block titles that CMS themes (Drupal, WordPress widgets) render as real headings.
NAV_HEADING = re.compile(
    r"^(social( media)? links?|social|(main|primary|secondary|top|utility|footer|header|mobile) (menu|navigation|links)"
    r"|menu|navigation|main navigation|breadcrumbs?|footer( bottom menus?| links| menu)?|follow us|connect with us"
    r"|search( for)?:?|site search|sidebar|skip to (main )?content|language( switcher)?)$", re.I)
PHONE_ONLY = re.compile(r"^[\d\s()+\-.]{6,}$")
EMPTY_META_WATCH = ("google-site-verification", "fb:app_id", "description", "keywords", "og:title",
                    "og:description", "og:image", "og:url")
_TOKEN = re.compile(r"[^\W\d_]+", re.U)


def _txt(el) -> str:
    return " ".join(el.get_text(" ").split()) if el else ""


def _has_directive(value: Optional[str], directive: str) -> bool:
    tokens = [t.strip() for t in re.split(r"[,:;]", (value or "").lower())]
    return directive in tokens or "none" in tokens


def _collect_types(obj, out: list) -> None:
    if isinstance(obj, list):
        for item in obj:
            _collect_types(item, out)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        for name in (t if isinstance(t, list) else [t]):
            if isinstance(name, str) and name not in out:
                out.append(name)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _collect_types(v, out)


def _detect_trackers(soup: BeautifulSoup) -> dict[str, list[str]]:
    parts = []
    for s in soup.find_all("script"):
        if s.get("src"):
            parts.append(s["src"])
        if s.string:
            parts.append(s.string)
    for tag in soup.find_all(["iframe", "img"]):
        src = tag.get("src") or ""
        if "googletagmanager" in src or "facebook.com/tr" in src:
            parts.append(src)
    blob = "\n".join(parts)
    blob_lower = blob.lower()
    found: dict[str, list[str]] = {}
    for name, rx in TRACKER_IDS.items():
        ids = sorted(set(rx.findall(blob)))
        if ids:
            found[name] = ids
    for name, markers in TRACKER_MARKERS.items():
        if name not in found and any(m in blob_lower for m in markers):
            found[name] = []
    return found


def _detect_platform(lower: str, generator: Optional[str], headers: dict) -> Optional[str]:
    probe = f"{lower} {(generator or '').lower()} {headers.get('x-powered-by', '').lower()}"
    for name, markers in PLATFORMS:
        if name.lower().split()[0] in (generator or "").lower():
            return name
        if any(m in probe for m in markers):
            return name
    cookies = headers.get("set-cookie", "").lower()
    for name, markers in COOKIE_PLATFORMS:
        if any(m in cookies for m in markers):
            return name
    powered = headers.get("x-powered-by", "")
    return f"Custom stack (X-Powered-By: {powered})" if powered else None


def _tel_countries(soup) -> list[str]:
    """Country codes of tel: / WhatsApp links ('+65 9225 9222' -> 'SG'), a country hint for .com sites."""
    out = []
    for a in soup.find_all("a", href=True):
        h = a["href"].strip().lower()
        num = None
        if h.startswith("tel:"):
            num = re.sub(r"[^\d+]", "", h[4:])
        elif "wa.me/" in h or "whatsapp.com/send" in h:
            m = re.search(r"(?:wa\.me/|phone=)(\d{8,15})", h)
            num = "+" + m.group(1) if m else None
        if num and num.startswith("00"):
            num = "+" + num[2:]
        if num and num.startswith("+"):
            code = next((c for p, c in PHONE_COUNTRY if num.startswith(p)), None)
            if code:
                out.append(code)
    return sorted(set(out))


def _short_name(name: str, limit: int = 40) -> str:
    return name if len(name) <= limit else name[:limit - 1] + "…"


ORG_TYPES = {"Organization", "Corporation", "LocalBusiness", "ProfessionalService", "NGO", "EducationalOrganization",
             "MedicalOrganization", "GovernmentOrganization", "Store", "OnlineBusiness"}
ORG_FIELDS = ("name", "url", "logo", "sameAs", "address", "contactPoint", "telephone", "description")


def _find_org(data) -> dict:
    """The first Organization-like JSON-LD node: its name, country and which key fields it fills."""
    stack = [data]
    while stack:
        node = stack.pop(0)
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            t = node.get("@type")
            types = set(t if isinstance(t, list) else [t])
            if types & ORG_TYPES:
                addr = node.get("address")
                addr = addr[0] if isinstance(addr, list) and addr else addr
                country = addr.get("addressCountry") if isinstance(addr, dict) else None
                if isinstance(country, dict):
                    country = country.get("name")
                name = node.get("name")
                return {"name": name if isinstance(name, str) else None,
                        "country": country if isinstance(country, str) else None,
                        "fields": [f for f in ORG_FIELDS if node.get(f)]}
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return {}


def _jsonld_issues(data, page_url: str) -> list[str]:
    """Defects inside structured data that validators flag or that leak internals."""
    issues = []
    https = page_url.startswith("https://")
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if isinstance(node, str):
            m = DEV_HOST.search(node)
            if m:
                issues.append(f"development/staging URL leaked: {m.group(0)}")
            continue
        if not isinstance(node, dict):
            continue
        t = node.get("@type")
        types = set(t if isinstance(t, list) else [t])
        if "FAQPage" in types and not node.get("mainEntity"):
            issues.append("FAQPage with no questions (empty mainEntity), invalid for rich results")
        author = node.get("author")
        for a in (author if isinstance(author, list) else [author]):
            name = a.get("name") if isinstance(a, dict) else a
            if isinstance(name, str) and name.strip().lower() in ("admin", "administrator", "user", "editor"):
                issues.append(f"author is '{name.strip()}'")
        logo = node.get("logo")
        logo = logo.get("url") if isinstance(logo, dict) else logo
        if isinstance(logo, str) and logo and not logo.startswith(("http://", "https://", "//")):
            issues.append(f"logo is a relative path ('{logo[:50]}')")
        if https and isinstance(node.get("url"), str) and node["url"].startswith("http://") and types & ORG_TYPES:
            issues.append(f"Organization url uses http:// ('{node['url'][:60]}')")
        geo = node.get("geo")
        if isinstance(geo, dict):
            for k in ("latitude", "longitude"):
                v = geo.get(k)
                if isinstance(v, str) and not re.fullmatch(r"-?\d+(\.\d+)?", v.strip()):
                    issues.append(f"geo {k} is not a number ('{v[:30]}')")
        for v in node.values():
            if isinstance(v, (dict, list)):
                stack.append(v)
            elif isinstance(v, str):
                m = DEV_HOST.search(v)
                if m:
                    issues.append(f"development/staging URL leaked: {m.group(0)}")
    return issues


HOSTING = (
    ("Pantheon", lambda h: "x-pantheon-styx-hostname" in h),
    ("WP Engine", lambda h: "wpe-backend" in h or "wp engine" in h.get("x-powered-by", "").lower()),
    ("Kinsta", lambda h: "x-kinsta-cache" in h),
    ("Vercel", lambda h: "x-vercel-id" in h),
    ("Netlify", lambda h: "x-nf-request-id" in h or "netlify" in h.get("server", "").lower()),
    ("Cloudflare", lambda h: "cf-ray" in h),
    ("Fastly", lambda h: "fastly" in (h.get("x-served-by", "") + h.get("via", "") + h.get("x-cache", "")).lower()
     or "x-fastly-request-id" in h),
    ("Akamai", lambda h: "ak_p" in h.get("server-timing", "") or "x-akamai-transformed" in h),
    ("AWS CloudFront", lambda h: "x-amz-cf-id" in h),
    ("Azure Front Door", lambda h: "x-azure-ref" in h),
)
SITE_TOOLS = (
    ("Rank Math", ("rank-math", "rankmath")), ("Yoast SEO", ("yoast-schema-graph", "yoast seo")),
    ("All in One SEO", ("aioseo",)), ("SEOPress", ("seopress",)), ("Elementor", ("elementor-",)),
    ("WPBakery", ("js_composer", "vc_row")), ("Divi", ("et_pb_",)), ("TranslatePress", ("translatepress",)),
    ("WPML", ("wpml-ls", "sitepress-multilingual")), ("Polylang", ("polylang",)), ("Smart Slider", ("smart-slider", "n2-ss-")),
    ("WP Rocket", ("wp-rocket",)), ("Smush", ("smush",)), ("Weglot", ("weglot",)),
    ("Bricks Builder", ("brxe-", "bricks-")), ("Oxygen", ("/oxygen/component-framework/",)), ("Beaver Builder", ("fl-builder",)),
)


def _stack(lower: str, headers: dict) -> dict:
    return {"hosting": [n for n, test in HOSTING if test(headers)],
            "tools": [n for n, marks in SITE_TOOLS if any(m in lower for m in marks)]}


def _leaky_headers(headers: dict) -> list[str]:
    """Response headers that expose debug state or exact software versions."""
    out = []
    for k, v in headers.items():
        if "debug" in k:
            out.append(f"{k}: {v}"[:80])
        elif k in ("x-powered-by", "server", "x-aspnet-version", "x-generator") and re.search(r"\d", v):
            out.append(f"{k}: {v}"[:80])
    return out


def _one_edit(a: str, b: str) -> bool:
    """True when a and b differ by exactly one substitution/insertion/deletion."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


COMMON_WORDS = frozenset("""
professional professionals experience experienced reliable reliability quality solutions solution services
installation installations maintenance management manufacturing engineering engineers construction commercial
industrial residential customers customer business businesses company companies government environment
environmental sustainable sustainability technology technologies innovation innovative digital simulation
guarantee guaranteed warranty certified certification accredited compliance compliant consultation consulting
consultant excellence efficiency efficient performance partnership partners delivery development developer
international national communication communications infrastructure transportation logistics healthcare
hospitality accommodation restaurant property properties investment financial finance insurance marketing
advertising strategy strategic analytics resources available availability affordable transparent transparency
responsible security safety secure trusted experts expertise solar energy electricity renewable generation
employment recruitment placement agency caregiver domestic helper helpers education training learning
contact appointment schedule support portfolio projects project testimonials reviews gallery pricing products
product categories category features benefits advantages overview mission vision history leadership
""".split())


_WORDS_BY_LEN: dict[int, list[str]] = {}
for _w in COMMON_WORDS:
    _WORDS_BY_LEN.setdefault(len(_w), []).append(_w)
REAL_NEIGHBOURS = frozenset({"contract", "contracts", "contacts", "products", "projected", "suppport"} - {"suppport"})


def _typos(texts: list[str], body_lower: str = "") -> list[str]:
    """Words one missing/extra letter away from a common business word ('Pofessional', 'Digitial').
    Only edits inside the word count, so real variants like 'reliably' or 'services' are not flagged;
    words repeated on the page are treated as intentional."""
    out = []
    for text in texts:
        for raw in _TOKEN.findall(text):
            w = raw.lower()
            if len(w) < 6 or w in COMMON_WORDS or w in REAL_NEIGHBOURS or body_lower.count(w) > 2:
                continue
            for good in _WORDS_BY_LEN.get(len(w) - 1, []) + _WORDS_BY_LEN.get(len(w) + 1, []):
                if good[:1] != w[:1] and good[1:2] != w[:1]:
                    continue
                short, long_ = sorted((w, good), key=len)
                pos = next((i for i in range(len(short)) if short[i] != long_[i]), len(short))
                if pos < len(short) - 1 and long_[:pos] + long_[pos + 1:] == short:
                    out.append(f"'{raw}' (should be '{good}') in \"{text[:60]}\"")
                    break
    return list(dict.fromkeys(out))[:3]


def _name_mismatches(headings: list[str], link_texts: list[str]) -> list[str]:
    """Headings that spell a name differently from the navigation ('MARIN LISA' vs 'Marin Liza')."""
    # Names are capitalised; comparing only capitalised words against short menu labels avoids pairing two
    # ordinary words ("atau"/"Atap", "lend"/"led") from long headlines.
    def caps(text: str) -> set[str]:
        return {w.casefold() for w in _TOKEN.findall(text) if w[:1].isupper()}

    link_bigrams: dict[tuple[str, str], str] = {}
    link_caps: dict[str, set[str]] = {}
    vocab = set()
    for t in link_texts:
        if len(t.split()) > 6:
            continue
        toks = _TOKEN.findall(t.casefold())
        vocab.update(toks)
        link_caps[t] = caps(t)
        for pair in zip(toks, toks[1:]):
            link_bigrams.setdefault(pair, t)
    out = []
    for h in headings:
        toks = _TOKEN.findall(h.casefold())
        h_caps = caps(h)
        for a, b in zip(toks, toks[1:]):
            if (a, b) in link_bigrams:
                continue
            for (la, lb), text in link_bigrams.items():
                hit = (a == la and b not in vocab and len(b) >= 4 and _one_edit(b, lb)
                       and b in h_caps and lb in link_caps[text]) or \
                      (b == lb and a not in vocab and len(a) >= 4 and _one_edit(a, la)
                       and a in h_caps and la in link_caps[text])
                plural = {a, b} & {la + "s", lb + "s"} or {la, lb} & {a + "s", b + "s"}
                if hit and not plural:
                    out.append(f"'{h}' (heading) vs '{text}' (link)")
                    break
    return list(dict.fromkeys(out))[:3]


def extract_page(url: str, final_url: Optional[str], html: str, headers: dict,
                 *, is_home: bool = False) -> PageFacts:
    base = final_url or url
    base_host = urlparse(base).hostname or ""
    soup = BeautifulSoup(html, "lxml")
    lower = html.lower()

    metas: dict[str, str] = {}
    og: dict[str, str] = {}
    tw: dict[str, str] = {}
    empty_meta: list[str] = []
    bad_meta_names: list[str] = []
    for m in soup.find_all("meta"):
        name = (m.get("name") or "").strip().lower()
        prop = (m.get("property") or "").strip().lower()
        if "::" in (name + prop) or " " in (name + prop):
            bad_meta_names.append(name or prop)
        content = (m.get("content") or "").strip()
        if (name or prop) in EMPTY_META_WATCH and m.has_attr("content") and not content:
            empty_meta.append(name or prop)
        if prop.startswith("og:"):
            og.setdefault(prop, content)
        if name.startswith("twitter:") or prop.startswith("twitter:"):
            tw.setdefault(name or prop, content)
        if name:
            metas.setdefault(name, content)

    title = _txt(soup.title) or None
    desc = metas.get("description") or None
    meta_robots = ", ".join(v for v in (metas.get("robots"), metas.get("googlebot")) if v) or None
    x_robots = headers.get("x-robots-tag")

    canonical, hreflang = None, []
    for link in soup.find_all("link", href=True):
        rels = [r.lower() for r in (link.get("rel") or [])]
        if "canonical" in rels and canonical is None:
            canonical = urljoin(base, link["href"].strip())
        if "alternate" in rels and link.get("hreflang"):
            hreflang.append(link["hreflang"])

    h1s = soup.find_all("h1")
    h1 = [t for t in (_txt(h) for h in h1s) if t]
    h2 = [t for t in (_txt(h) for h in soup.find_all("h2")) if t][:15]
    heading_els = soup.find_all(re.compile(r"^h[1-6]$"))
    levels = [int(h.name[1]) for h in heading_els]
    skips = sum(1 for a, b in zip(levels, levels[1:]) if b > a + 1)
    headings = [(int(h.name[1]), _txt(h)) for h in heading_els]
    headings = [(lv, t) for lv, t in headings if t]
    nav_headings = [f"H{lv}: {t}" for lv, t in headings if NAV_HEADING.match(t)]
    content_headings = [(lv, t) for lv, t in headings if not NAV_HEADING.match(t)]
    top_heading = f"H{content_headings[0][0]}: {content_headings[0][1][:100]}" if content_headings else None
    minor = list(dict.fromkeys(t[:80] for lv, t in headings if lv >= 3))[:40]
    per_level = Counter(lv for lv, _ in headings)
    faq_like = sum(1 for _, t in headings if t.rstrip().endswith("?")) + \
        sum(1 for el in soup.find_all(["summary", "dt", "button"]) if _txt(el).rstrip().endswith("?"))

    imgs = soup.find_all("img")
    missing_alt = sum(1 for i in imgs if not i.has_attr("alt"))
    empty_alt = sum(1 for i in imgs if i.has_attr("alt") and not str(i["alt"]).strip())
    missing_alt_srcs = list(dict.fromkeys(
        _short_name(urlparse(i.get("src") or i.get("data-src") or "").path.rsplit("/", 1)[-1] or "(no src)")
        for i in imgs if not i.has_attr("alt")))[:10]

    internal = external = nofollow = 0
    ext_domains: Counter = Counter()
    link_texts, link_prefixes, internal_paths = [], set(), []
    empty_hrefs = spaced_hrefs = internal_nofollow = 0
    link_targets: set[str] = set()
    generic_anchors: list[tuple[str, str]] = []
    broken_email_links: list[str] = []
    spaced_examples: list[str] = []
    for a in soup.find_all("a", href=True):
        raw_href = a["href"]
        if not raw_href.strip():
            empty_hrefs += 1
            continue
        if EMAIL_ONLY.fullmatch(raw_href.strip()):
            # An email address used as a URL: the browser resolves it as a relative page and 404s.
            broken_email_links.append(f"href='{raw_href.strip()}' (text: '{_txt(a)[:40]}')")
            continue
        if raw_href != raw_href.strip() and not raw_href.strip().startswith("#"):
            spaced_hrefs += 1
            spaced_examples.append(repr(raw_href)[:60])
        href = raw_href.strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base, href)
        host = urlparse(absolute).hostname
        if not host:
            continue
        if bare_host(host) == bare_host(base_host):
            internal += 1
            text = _txt(a)
            if text:
                link_texts.append(text)
            lp = locale_prefix(absolute)
            if lp:
                link_prefixes.add(lp)
            path = urlparse(absolute).path.rstrip("/")
            if path and is_home:
                internal_paths.append(path)
            link_targets.add(path or "/")
            if text and GENERIC_ANCHOR.match(text):
                generic_anchors.append((text[:30], path or "/"))
            if "nofollow" in [r.lower() for r in (a.get("rel") or [])]:
                internal_nofollow += 1
        else:
            external += 1
            ext_domains[bare_host(host)] += 1
        if "nofollow" in [r.lower() for r in (a.get("rel") or [])]:
            nofollow += 1

    types: list[str] = []
    blocks = invalid = 0
    org: dict = {}
    ld_issues: list[str] = []
    for s in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        blocks += 1
        try:
            data = json.loads(s.string or s.get_text() or "")
            _collect_types(data, types)
            org = org or _find_org(data)
            ld_issues.extend(_jsonld_issues(data, base))
        except (ValueError, TypeError):
            invalid += 1
    for el in soup.find_all(attrs={"itemtype": True}):
        t = str(el["itemtype"]).rstrip("/").split("/")[-1]
        if t and t not in types:
            types.append(t)

    trackers = _detect_trackers(soup)
    platform = _detect_platform(lower, metas.get("generator"), headers) if is_home else None
    legacy = sorted({m.group(0).lower() for s in soup.find_all("script", src=True)
                     for m in [LEGACY_JS.search(s["src"])] if m})
    counters = sum(1 for el in soup.find_all(attrs={"data-count": True}) if _txt(el) in ("0", "0+")) + \
        sum(1 for el in soup.find_all(attrs={"data-target": re.compile(r"^\d+$")}) if _txt(el) in ("0", "0+"))

    for t in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
        t.decompose()
    body = soup.body or soup
    text_nodes = [" ".join(s.split()) for s in body.stripped_strings]
    text_nodes = [t for t in text_nodes if t]
    body_text = " ".join(text_nodes)
    word_count = len(body_text.split())
    text_lower = body_text.lower()

    signals: dict = {}
    if empty_meta:
        signals["empty_meta"] = sorted(set(empty_meta))
    if legacy:
        signals["legacy_scripts"] = legacy
    if og.get("og:site_name", "").lower().startswith(("http://", "https://", "www.")):
        signals["og_site_name_url"] = og["og:site_name"]
    stray = [t for t in text_nodes if "-->" in t or "<!--" in t]
    if stray:
        signals["stray_comment"] = stray[0][:60]
    zero = max(len(ZERO_COUNTER.findall(body_text)), counters)
    if zero >= 2:
        signals["zero_counters"] = zero
    ph = sorted({m.group(0) for m in PLACEHOLDER.finditer(body_text)} |
                {m.group(0) for a in soup.find_all("a", href=True) for m in [PLACEHOLDER.search(a["href"])] if m})
    if ph:
        signals["placeholder"] = ph[:5]
    years = [int(y) for y in COPYRIGHT_YEAR.findall(body_text)]
    if years and max(years) < date.today().year - 1:
        signals["stale_copyright"] = max(years)
    misuse = [f"H{lv}: {t[:60]}" for lv, t in headings
              if "©" in t or "copyright" in t.lower() or PHONE_ONLY.match(t)]
    if misuse:
        signals["heading_misuse"] = misuse[:3]
    minor_count = per_level[5] + per_level[6]
    if minor_count >= 8 and minor_count > 2 * (per_level[2] + per_level[3]):
        signals["layout_headings"] = minor_count
    if h1 and h2 and h1[0].casefold() == h2[0].casefold():
        signals["h1_equals_h2"] = h1[0][:80]
    if title and BARE_TITLE.search(title):
        signals["bare_title"] = True
    if ld_issues:
        signals["jsonld_issues"] = list(dict.fromkeys(ld_issues))[:5]
    vp = (metas.get("viewport") or "").lower().replace(" ", "")
    if "user-scalable=no" in vp or "user-scalable=0" in vp or re.search(r"maximum-scale=1(\.0)?(,|$)", vp):
        signals["zoom_blocked"] = metas.get("viewport")
    if detect_language(body_text) == "en":
        bad_numbers = sorted(set(MALFORMED_NUMBER.findall(body_text)))
        if bad_numbers:
            signals["malformed_numbers"] = bad_numbers[:3]
    ext = sorted({m.group(0) for m in EXTENSION_ATTR.finditer(lower)})
    if ext:
        signals["extension_attrs"] = ext
    if empty_hrefs + spaced_hrefs >= 2:
        signals["bad_hrefs"] = {"empty": empty_hrefs, "whitespace": spaced_hrefs, "examples": spaced_examples[:2]}
    if broken_email_links:
        signals["broken_email_links"] = list(dict.fromkeys(broken_email_links))[:3]
    if bad_meta_names:
        signals["malformed_meta"] = sorted(set(bad_meta_names))[:3]
    if "gtm_auth=" in lower and "gtm_preview=env-" in lower:
        signals["gtm_environment"] = True
    if is_home and title and GENERIC_HOME_TITLE.match(title):
        signals["generic_home_title"] = title
    if nav_headings:
        signals["nav_headings"] = nav_headings[:6]
    if desc and len(desc) <= 3:
        signals["desc_placeholder"] = desc
    typos = _typos(([title] if title else []) + [t for _, t in headings], text_lower) \
        if detect_language(body_text) == "en" else []
    if typos:
        signals["typos"] = typos
    mismatch = _name_mismatches([t for _, t in headings], link_texts)
    if mismatch:
        signals["name_mismatch"] = mismatch
    if is_home:
        leaky = _leaky_headers(headers)
        if leaky:
            signals["leaky_headers"] = leaky
        signals["stack"] = _stack(lower, headers)

    consent = next((n for n, marks in CONSENT_VENDORS if any(m in lower for m in marks)), None)
    if consent is None and "cookie" in text_lower and any(p in text_lower for p in CONSENT_TEXT):
        consent = "heuristic:banner_text"

    main = soup.find("main") or soup.find("article")
    if main is None:
        for t in body.find_all(["nav", "footer", "header"]):
            t.decompose()
        main = body
    excerpt = " ".join(main.get_text(" ").split())[:1500]

    page_type = "home" if is_home else classify_path(urlparse(base).path)[0]
    return PageFacts(
        url=base, page_type=page_type, title=title, title_length=len(title or ""),
        meta_description=desc, meta_description_length=len(desc or ""), canonical=canonical,
        canonical_is_self=(normalize_url(canonical) == normalize_url(base)) if canonical else None,
        meta_robots=meta_robots, x_robots_tag=x_robots,
        noindex=_has_directive(meta_robots, "noindex") or _has_directive(x_robots, "noindex"),
        lang=(soup.html.get("lang") if soup.html else None) or None,
        content_language=detect_language(body_text),
        viewport=metas.get("viewport") or None, hreflang=hreflang, og=og, twitter=tw,
        h1=h1, h1_count=len(h1s), h1_empty=len(h1s) - len(h1), top_heading=top_heading, minor_headings=minor,
        h2=h2, h3_count=per_level[3], heading_skips=skips,
        word_count=word_count, word_count_raw=word_count, images_total=len(imgs), images_missing_alt=missing_alt,
        images_empty_alt=empty_alt, missing_alt_srcs=missing_alt_srcs, internal_links=internal,
        link_locale_prefixes=sorted(link_prefixes), external_links=external,
        external_domains=[d for d, _ in ext_domains.most_common(15)],
        internal_paths=list(dict.fromkeys(internal_paths))[:80],
        link_targets=sorted(link_targets)[:400], generic_anchors=[list(g) for g in generic_anchors[:60]],
        internal_nofollow=internal_nofollow, tel_prefixes=_tel_countries(soup),
        faq_like=faq_like, org_name=org.get("name"), org_country=org.get("country"),
        org_fields=org.get("fields", []),
        nofollow_links=nofollow, schema_types=types, jsonld_blocks=blocks, jsonld_invalid=invalid,
        trackers=trackers, consent_vendor=consent, platform=platform,
        generator=metas.get("generator") or None, signals=signals, text_excerpt=excerpt,
        text_nodes=text_nodes)


def _ratio(affected: int, checked: int) -> str:
    if checked == 0:
        return "not_checked"
    if affected == 0:
        return "present"
    return "missing" if affected == checked else "partial"


def _examples(urls: list[str]) -> list[str]:
    return urls[:5]


def _text_check(facts: list[PageFacts], home: PageFacts, attr: str, lo: int, hi: int) -> Check:
    values = [(f.url, getattr(f, attr)) for f in facts]
    missing = [u for u, v in values if not v]
    too_long = [u for u, v in values if v and len(v) > hi]
    too_short = [u for u, v in values if v and len(v) < lo]
    counts = Counter(v for _, v in values if v)
    dupes = [u for u, v in values if v and counts[v] > 1]
    affected = len(set(missing + too_long + too_short + dupes))
    if len(missing) == len(values):
        status = "missing"
    else:
        status = "partial" if affected else "present"
    flag = "bare_title" if attr == "title" else "desc_placeholder"
    placeholder = [f for f in facts if f.signals.get(flag)]
    # A brand suffix the template appends to every value (" | Schneider Electric") and the over-length
    # values it causes on its own.
    suffix, suffix_pages, suffix_overflow = None, 0, 0
    tails = Counter(m.group(0) for _, v in values if v for m in [TEMPLATE_SUFFIX.search(v)] if m)
    if tails:
        tail, c = tails.most_common(1)[0]
        if c >= max(3, len(values) // 2):
            suffix, suffix_pages = tail, c
            suffix_overflow = sum(1 for _, v in values if v and v.endswith(tail) and len(v) > hi >= len(v) - len(tail))
    return Check(status=status, value=getattr(home, attr), pages_checked=len(values),
                 pages_affected=affected,
                 details={"missing": len(missing), "too_long": len(too_long), "too_short": len(too_short),
                          "duplicates": len(dupes), "recommended_length": f"{lo}-{hi}",
                          "homepage_length": len(getattr(home, attr) or ""),
                          "duplicate_groups": [{"value": v[:120], "pages": c} for v, c in counts.most_common(3) if c > 1],
                          "placeholder": len(placeholder),
                          "placeholder_values": sorted({(f.title if attr == "title" else f.meta_description) or ""
                                                       for f in placeholder})[:3],
                          "examples_placeholder": _examples([f.url for f in placeholder]),
                          "examples_missing": _examples(missing), "examples_too_long": _examples(too_long),
                          "examples_too_long_values": [v[:100] for _, v in values if v and len(v) > hi][:2],
                          "template_suffix": suffix, "template_suffix_pages": suffix_pages,
                          "over_limit_only_because_of_suffix": suffix_overflow,
                          "homepage_generic": home.signals.get("generic_home_title") if attr == "title" else None})


def _broken_links(crawl: CrawlSummary) -> Check:
    broken = crawl.broken_urls
    checked = max(0, crawl.pages_attempted - 1)
    if checked == 0:
        return not_checked(crawl.reason or "no_pages_attempted")
    sm = crawl.sitemap
    from_sitemap = [b.url for b in broken if b.source in ("sitemap", "both")] + sm.probe_dead_examples
    dead_in_sitemap = len(from_sitemap) - len(sm.probe_dead_examples) + sm.probe_dead
    from_links = [b.url for b in broken if b.source in ("homepage_link", "both")]
    fetched = crawl.sitemap_urls_fetched + sm.probe_checked
    dead = len(broken) + sm.probe_dead
    checked += sm.probe_checked
    # Dead URLs explained by a wrong locale prefix are a sitemap-generator bug, not broken links on the site.
    explained = [u for u in dict.fromkeys(from_sitemap) if locale_prefix(u) in sm.prefix_fixes]
    affected_by_prefix = sum(sm.prefix_counts.get(p, 0) for p in sm.prefix_fixes)
    diagnosis = None
    if explained and len(explained) >= len(set(from_sitemap)) - 1 and not from_links:
        diagnosis = "sitemap_wrong_locale_prefix"
    return Check(
        status="partial" if dead else "present", value=dead, pages_checked=checked,
        pages_affected=dead,
        details={"in_sitemap": dead_in_sitemap, "linked_from_homepage": len(from_links),
                 "sitemap_probe_checked": sm.probe_checked, "sitemap_probe_dead": sm.probe_dead,
                 "sitemap_urls_fetched": fetched,
                 "sitemap_dead_ratio": round(dead_in_sitemap / fetched, 2) if fetched else None,
                 "diagnosis": diagnosis, "prefix_fixes": sm.prefix_fixes,
                 "sitemap_urls_with_dead_prefix": affected_by_prefix,
                 "examples_sitemap": _examples(from_sitemap), "examples_linked": _examples(from_links)},
        note=(f"{dead} of {checked} URLs checked return 404/410. Only a sample was fetched, "
              "so the site may have more") if dead else None)


def boilerplate_text(facts: list[PageFacts]) -> set[str]:
    """Text nodes repeated on at least half of the pages (menus, footers, cookie notes)."""
    n = len(facts)
    if n < 3:
        return set()
    presence = Counter(t for f in facts for t in set(f.text_nodes))
    threshold = max(3, math.ceil(0.5 * n))
    return {t for t, c in presence.items() if c >= threshold}


def _strip_boilerplate(facts: list[PageFacts]) -> None:
    """Drop text repeated across most pages (menus, footers) so word counts and excerpts reflect the
    page's own content. Works on sites that do not use <nav>/<footer> tags."""
    boiler = boilerplate_text(facts)
    if not boiler:
        return
    for f in facts:
        own = [t for t in f.text_nodes if t not in boiler]
        f.word_count = len(" ".join(own).split())
        if own:
            f.text_excerpt = " ".join(own)[:1500]


def _mark_empty_templates(facts: list[PageFacts], home: PageFacts, crawl: CrawlSummary) -> None:
    """Pages that are the CMS's empty shell (what an unknown URL returns), not real content."""
    probe_titles = {p.title for p in crawl.not_found_probes
                    if p.status_code == 200 and p.title and p.title != home.title}
    for f in facts:
        if f is home:
            continue
        f.empty_template = bool(
            (f.title and f.title in probe_titles)
            or (f.signals.get("bare_title") and not f.h1)
            or (not f.h1 and f.h1_empty and f.word_count < 25))


def rollup(records: list[PageRecord], crawl: CrawlSummary) -> tuple[TechnicalSEO, ContentStats, Locale]:
    facts = [r.facts for r in records if r.facts]
    home = records[0].facts if records and records[0].facts else None
    reason = crawl.reason or "no_usable_pages"
    org_country = next((f.org_country for f in facts if f.org_country), None)
    phones = Counter(c for f in facts for c in f.tel_prefixes)
    phone_country = phones.most_common(1)[0][0] if phones else None
    locale = site_locale(crawl.final_domain or "", home.lang if home else None,
                         [f.content_language for f in facts], home.content_language if home else None,
                         org_country, phone_country)

    checks: dict[str, Check] = {}
    page_checks = ("meta_title", "meta_description", "open_graph", "twitter_card", "h1",
                   "heading_hierarchy", "image_alt_text", "schema_markup", "canonical", "indexability",
                   "mobile_viewport", "lang", "hreflang", "analytics", "cookie_banner", "platform")
    if not facts or home is None:
        for name in page_checks:
            checks[name] = not_checked(reason if not facts else "homepage_not_usable")
    else:
        n = len(facts)
        _strip_boilerplate(facts)
        _mark_empty_templates(facts, home, crawl)
        checks["meta_title"] = _text_check(facts, home, "title", 30, 60)
        checks["meta_description"] = _text_check(facts, home, "meta_description", 70, 160)

        og_bad = [f.url for f in facts if not (f.og.get("og:title") and f.og.get("og:description"))]
        # Tags that exist but are template constants: every share card then shows / links to the homepage.
        og_url_wrong = [f.url for f in facts if f is not home and f.og.get("og:url")
                        and normalize_url(f.og["og:url"], base=f.url) != normalize_url(f.url)]
        og_titles = Counter(f.og.get("og:title") for f in facts if f.og.get("og:title"))
        titles = {f.title for f in facts if f.title}
        const_title = None
        if n >= 3 and len(og_titles) == 1 and len(titles) > 1:
            const_title = next(iter(og_titles))
        og_images = Counter(f.og["og:image"].rsplit("/", 1)[-1].split("?")[0] for f in facts if f.og.get("og:image"))
        img, img_pages = og_images.most_common(1)[0] if og_images else (None, 0)
        reused_image = {"image": _short_name(img), "pages": img_pages} if img and n >= 4 and img_pages >= 3             and len(titles) > 2 else None
        og_status = _ratio(len(og_bad), n)
        if og_status == "present" and (not home.og.get("og:image") or og_url_wrong or const_title
                                       or home.signals.get("og_site_name_url") or reused_image):
            og_status = "partial"
        checks["open_graph"] = Check(
            status=og_status, pages_checked=n, pages_affected=len(set(og_bad + og_url_wrong)),
            value=sorted(home.og.keys()),
            details={"homepage_og_image": bool(home.og.get("og:image")), "examples_missing": _examples(og_bad),
                     "og_url_not_self": len(og_url_wrong), "og_url_value": home.og.get("og:url"),
                     "examples_og_url_not_self": _examples(og_url_wrong),
                     "constant_og_title": const_title,
                     "og_site_name_is_url": home.signals.get("og_site_name_url"),
                     "reused_og_image": reused_image,
                     "twitter_card": home.twitter.get("twitter:card")},
            note=None if og_status == "present" else "Social shares fall back to platform defaults without og tags")

        tw_bad = [f.url for f in facts if not f.twitter.get("twitter:card")]
        checks["twitter_card"] = Check(status=_ratio(len(tw_bad), n), pages_checked=n,
                                       pages_affected=len(tw_bad), value=home.twitter.get("twitter:card"))

        # An empty <h1></h1> counts as no H1: it gives search engines nothing.
        no_h1 = [f.url for f in facts if not f.h1]
        empty_h1 = [f.url for f in facts if not f.h1 and f.h1_empty]
        multi_h1 = [f.url for f in facts if len(f.h1) > 1]
        h1_status = "missing" if len(no_h1) == n else ("partial" if no_h1 or multi_h1 else "present")
        checks["h1"] = Check(status=h1_status, value=home.h1, pages_checked=n,
                             pages_affected=len(no_h1) + len(multi_h1),
                             details={"missing": len(no_h1), "empty_tag": len(empty_h1), "multiple": len(multi_h1),
                                      "homepage_top_heading": home.top_heading,
                                      "examples_missing": _examples(no_h1), "examples_empty": _examples(empty_h1),
                                      "examples_multiple": _examples(multi_h1)})

        skip_pages = [f.url for f in facts if f.heading_skips]
        hh = _ratio(len(skip_pages), n)
        checks["heading_hierarchy"] = Check(
            status="partial" if hh == "missing" else hh, pages_checked=n, pages_affected=len(skip_pages),
            details={"examples": _examples(skip_pages)},
            note="Pages that jump heading levels (e.g. H2 -> H4)" if skip_pages else None)

        total_img = sum(f.images_total for f in facts)
        missing_alt = sum(f.images_missing_alt for f in facts)
        alt_pages = [f.url for f in facts if f.images_missing_alt]
        alt_status = "present" if total_img == 0 else _ratio(missing_alt, total_img)
        # One image missing alt on most pages is a single fix in the shared template.
        src_pages = Counter(s for f in facts for s in f.missing_alt_srcs)
        template_imgs = [{"src": s, "pages": c} for s, c in src_pages.most_common(3)
                         if c >= 3 and c >= 0.5 * n]
        checks["image_alt_text"] = Check(
            status=alt_status, pages_checked=n, pages_affected=len(alt_pages),
            details={"images_total": total_img, "images_missing_alt": missing_alt,
                     "images_empty_alt": sum(f.images_empty_alt for f in facts),
                     "template_images": template_imgs,
                     "other_missing_srcs": [s for s, c in src_pages.most_common(6)
                                            if s not in {t["src"] for t in template_imgs}][:4],
                     "examples": _examples(alt_pages)},
            note="No <img> tags found" if total_img == 0 else
            "Empty alt=\"\" is valid for decorative images and is not counted as missing")

        type_counts = Counter(t for f in facts for t in f.schema_types)
        posts = [f for f in facts if f.page_type == "blog_post"]
        article_types = {"Article", "BlogPosting", "NewsArticle", "TechArticle"}
        schema_status = "missing" if not type_counts else ("present" if home.schema_types else "partial")
        org_page = next((f for f in facts if f.org_fields), None)
        org_missing = [k for k in ("logo", "sameAs", "address", "contactPoint") if org_page and k not in org_page.org_fields]
        faq_no_schema = [f.url for f in facts if f.faq_like >= 3 and "FAQPage" not in f.schema_types]
        checks["schema_markup"] = Check(
            status=schema_status, value=home.schema_types,
            pages_checked=n, pages_affected=sum(1 for f in facts if not f.schema_types),
            details={"types_found": dict(type_counts.most_common()),
                     "organization_name": org_page.org_name if org_page else None,
                     "organization_missing_fields": org_missing,
                     "faq_content_without_faqpage": len(faq_no_schema),
                     "examples_faq_without_schema": _examples(faq_no_schema),
                     "faq_questions_on_first": next((f.faq_like for f in facts if f.url in faq_no_schema), 0),
                     "has_organization": any(t in type_counts for t in ("Organization", "LocalBusiness", "Corporation")),
                     "has_website": "WebSite" in type_counts,
                     "has_breadcrumbs": "BreadcrumbList" in type_counts,
                     "blog_posts_with_article_schema": sum(1 for f in posts if article_types & set(f.schema_types)),
                     "blog_posts_checked": len(posts),
                     "invalid_jsonld_blocks": sum(f.jsonld_invalid for f in facts)})

        no_canon = [f.url for f in facts if not f.canonical]
        non_self = [f.url for f in facts if f.canonical and f.canonical_is_self is False]
        cross = [f.url for f in facts if f.canonical and bare_host(urlparse(f.canonical).hostname)
                 != bare_host(urlparse(f.url).hostname)]
        checks["canonical"] = Check(
            status=_ratio(len(no_canon), n), value=home.canonical, pages_checked=n,
            pages_affected=len(no_canon),
            details={"missing": len(no_canon), "non_self_referencing": len(non_self),
                     "cross_domain": len(cross), "examples_missing": _examples(no_canon),
                     "examples_non_self": _examples(non_self)})

        noindex_pages = [f.url for f in facts if f.noindex]
        g_blocked = bool(crawl.robots.blocks_googlebot)
        if home.noindex or g_blocked:
            idx_status = "missing"
        else:
            idx_status = "partial" if noindex_pages else "present"
        checks["indexability"] = Check(
            status=idx_status, value=not (home.noindex or g_blocked), pages_checked=n,
            pages_affected=len(noindex_pages),
            details={"homepage_noindex": home.noindex, "robots_blocks_googlebot": g_blocked,
                     "noindex_pages": _examples(noindex_pages)},
            note="Homepage cannot be indexed by Google" if idx_status == "missing" else None)

        vp = (home.viewport or "").lower()
        checks["mobile_viewport"] = Check(
            status="present" if "width=device-width" in vp else ("partial" if vp else "missing"),
            value=home.viewport, details={"zoom_blocked": bool(home.signals.get("zoom_blocked"))})
        mismatched = [f.url for f in facts if f.lang and f.content_language
                      and primary(f.lang) != f.content_language]
        home_mismatch = home.url in mismatched
        lang_status = "missing" if not home.lang else ("partial" if mismatched else "present")
        checks["lang"] = Check(
            status=lang_status, value=home.lang, pages_checked=n, pages_affected=len(mismatched),
            details={"content_language": locale.content_language, "homepage_mismatch": home_mismatch,
                     "examples": _examples(mismatched),
                     "mismatch_examples": [{"url": f.url, "declared": f.lang, "detected": f.content_language}
                                           for f in facts if f.url in mismatched][:3]},
            note=(f"lang attribute does not match the detected content language on "
                  f"{len(mismatched)} page(s)") if mismatched else None)
        hl = sorted({h for f in facts for h in f.hreflang})
        prefixes = sorted({p for f in facts for p in f.link_locale_prefixes} |
                          {p for f in facts for p in [locale_prefix(f.url)] if p})
        multilingual = len({primary(p) for p in prefixes}) >= 2
        lang_by_prefix = {p: sorted({f.lang or "none" for f in facts if locale_prefix(f.url) == p})
                          for p in prefixes}
        in_sitemap = crawl.sitemap.hreflang_annotations
        checks["hreflang"] = Check(
            status="present" if hl or in_sitemap else "missing", value=hl,
            details={"multilingual": multilingual, "locale_prefixes": prefixes, "in_sitemap": in_sitemap,
                     "html_lang_by_prefix": {k: v for k, v in lang_by_prefix.items() if v},
                     "x_default_missing": bool(hl) and "x-default" not in hl},
            note=None if hl or in_sitemap else ("Site has language versions (" + ", ".join(f"/{p}/" for p in prefixes)
                                  + ") but no hreflang links them" if multilingual else
                                  "Only needed for multi-language / multi-region sites (not scored)"))

        trackers: dict[str, set] = {}
        coverage: dict[str, int] = Counter()
        for f in facts:
            for k, ids in f.trackers.items():
                trackers.setdefault(k, set()).update(ids)
                coverage[k] += 1
        tracker_out = {k: sorted(v) for k, v in sorted(trackers.items())}
        tm = crawl.tag_manager
        via_gtm = {k: v for k, v in (tm.ids if tm and tm.fetched else {}).items() if k not in tracker_out or
                   set(v) - set(tracker_out.get(k, []))}
        every = set(tracker_out) | set(via_gtm)
        checks["analytics"] = Check(
            status="present" if every else "missing", value=sorted(every),
            pages_checked=n,
            details={"ids": tracker_out,
                     "coverage": {k: {"pages": coverage[k], "on_homepage": k in home.trackers} for k in tracker_out},
                     "gtm_container": tm.container if tm else None, "gtm_container_read": bool(tm and tm.fetched),
                     "via_gtm": via_gtm,
                     "dead_universal_analytics": sorted(set(tracker_out.get("universal_analytics", []))
                                                        | set(via_gtm.get("universal_analytics", []))),
                     "gtm_environment_snippet": bool(home.signals.get("gtm_environment")),
                     "google_analytics": bool({"ga4", "universal_analytics"} & every)})

        vendors = sorted({f.consent_vendor for f in facts if f.consent_vendor})
        ad_trackers = [k for k in sorted(every) if k not in ("plausible", "universal_analytics", "gtm")]
        checks["cookie_banner"] = Check(
            status="present" if vendors else "missing", value=vendors[0] if vendors else None,
            details={"vendors": vendors, "trackers_loaded": ad_trackers,
                     "tracking_without_detected_consent": bool(ad_trackers) and not vendors,
                     "privacy_law": locale.privacy_law or GENERIC_LAW},
            note="Detected from static HTML. Banners injected purely via JavaScript/GTM may be missed."
            if not vendors else None)

        stack = home.signals.get("stack") or {}
        checks["platform"] = Check(status="present" if home.platform else "missing", value=home.platform,
                                   details={"generator": home.generator, "hosting": stack.get("hosting", []),
                                            "tools": stack.get("tools", [])})

    r = crawl.robots
    robots_details = r.model_dump(exclude={"status"})
    if r.status == "present":
        bad = r.disallows_all or r.blocks_googlebot
        checks["robots_txt"] = Check(status="partial" if bad else "present", value=r.url, details=robots_details,
                                     note="robots.txt blocks crawlers from the homepage" if bad else None)
    elif r.status == "missing":
        checks["robots_txt"] = Check(status="missing", details=robots_details,
                                     note="Crawlers treat a missing robots.txt as allow-all, but you lose the sitemap reference")
    elif r.status == "error":
        checks["robots_txt"] = Check(status="error", details=robots_details, note="robots.txt returned a 5xx error")
    else:
        checks["robots_txt"] = not_checked(r.status if r.status != "not_checked" else reason)

    s = crawl.sitemap
    sm_map = {"present": "present", "invalid": "partial", "missing": "missing"}
    if s.status in sm_map:
        checks["sitemap"] = Check(status=sm_map[s.status], value=s.url, details=s.model_dump(exclude={"status"}),
                                  note="A file exists but it is not a valid XML sitemap" if s.status == "invalid" else None)
    else:
        checks["sitemap"] = not_checked(s.status if s.status != "not_checked" else reason)

    h = crawl.https
    if h is None:
        checks["https"] = not_checked(reason)
    elif h.final_scheme == "https":
        checks["https"] = Check(
            status="partial" if h.http_redirects_to_https is False else "present",
            value=True, details=h.model_dump(),
            note="HTTP version does not redirect to HTTPS" if h.http_redirects_to_https is False else None)
    else:
        checks["https"] = Check(status="missing", value=False, details=h.model_dump(),
                                note="Site is served over plain HTTP" + (" (HTTPS has a certificate error)" if h.ssl_error else ""))

    checks["broken_links"] = _broken_links(crawl)
    checks["not_found_handling"] = _not_found_check(crawl, facts)
    if facts and home is not None:
        checks["internal_linking"] = _internal_linking(facts, home)
    tech = TechnicalSEO(**checks)

    real = [f for f in facts if not f.empty_template]
    types = Counter(f.page_type for f in real)
    thin = [f for f in real if f.word_count < 300 and f.page_type not in ("contact", "legal", "home")]
    content = ContentStats(
        pages_analyzed=len(facts), empty_template_pages=len(facts) - len(real),
        avg_word_count=int(sum(f.word_count for f in real) / len(real)) if real else 0,
        thin_pages=len(thin), page_types=dict(types.most_common()),
        blog_detected=bool(types.get("blog_index") or types.get("blog_post") or (s.blog_url_count or 0)),
        blog_posts_crawled=types.get("blog_post", 0), sitemap_blog_urls=s.blog_url_count)
    return tech, content, locale


def _internal_linking(facts: list[PageFacts], home: PageFacts) -> Check:
    """Link graph of the sample: pages nothing else links to, generic anchors, card grids that all
    point to one listing page instead of the individual items."""
    def path(u: str) -> str:
        return (urlparse(u).path.rstrip("/") or "/")

    n = len(facts)
    unlinked = [f.url for f in facts if f is not home and not f.empty_template
                and not any(path(f.url) in g.link_targets for g in facts if g is not f)]
    generic = Counter(t.lower() for f in facts for t, _ in f.generic_anchors)
    generic_total = sum(generic.values())
    internal_total = sum(f.internal_links for f in facts)
    funnels = []
    for f in facts:
        by_target = Counter(target for _, target in f.generic_anchors)
        for target, count in by_target.most_common(1):
            text = next(t for t, tg in f.generic_anchors if tg == target)
            if count >= 3:
                funnels.append({"page": path(f.url), "anchor": text, "target": target, "count": count})
    share = round(generic_total / internal_total, 2) if internal_total else 0
    bad = bool(funnels) or len(unlinked) >= 2 or share >= 0.2
    return Check(status="partial" if bad else "present", value=round(internal_total / n) if n else 0,
                 pages_checked=n, pages_affected=len(unlinked),
                 details={"avg_internal_links": round(internal_total / n) if n else 0,
                          "unlinked_in_sample": len(unlinked), "examples_unlinked": _examples(unlinked),
                          "generic_anchor_links": generic_total, "generic_anchor_share": share,
                          "top_generic_anchors": [{"text": t, "count": c} for t, c in generic.most_common(3)],
                          "card_funnels": sorted(funnels, key=lambda x: -x["count"])[:3],
                          "internal_nofollow": sum(f.internal_nofollow for f in facts)})


def _not_found_check(crawl: CrawlSummary, facts: list[PageFacts]) -> Check:
    probes = crawl.not_found_probes
    if not probes:
        return not_checked("not_probed")
    soft = [p for p in probes if p.status_code == 200]
    shells = [f.url for f in facts if f.empty_template]
    details = {"probes": [p.model_dump() for p in probes], "empty_template_pages": len(shells),
               "examples_empty_template": _examples(shells)}
    if soft:
        p = soft[-1]
        return Check(status="missing", value=p.status_code, pages_checked=len(facts), pages_affected=len(shells),
                     details=details,
                     note=f"Unknown URL {p.url} returns HTTP 200 (title '{p.title or ''}') instead of 404")
    return Check(status="partial" if shells else "present", value=probes[0].status_code,
                 pages_checked=len(facts), pages_affected=len(shells), details=details)


def _short(url: str) -> str:
    p = urlparse(url)
    return p.path or "/"


def build_findings(records: list[PageRecord], crawl: CrawlSummary, tech: TechnicalSEO) -> list[Finding]:
    """Site-specific observations the generic checks do not express. Deterministic, evidence attached."""
    facts = [r.facts for r in records if r.facts]
    home = records[0].facts if records and records[0].facts else None
    if not facts or home is None:
        return []
    out: list[Finding] = []

    def pages_with(key: str) -> list[PageFacts]:
        return [f for f in facts if f.signals.get(key)]

    def add(type_, severity, message, evidence=None, pages=0):
        out.append(Finding(type=type_, severity=severity, message=message, evidence=evidence, pages=pages))

    hit = pages_with("zero_counters")
    if hit:
        f = hit[0]
        add("zero_counters", "medium",
            f"{f.signals['zero_counters']} statistic counters render as '0 +' in the HTML of {'the homepage' if f is home else _short(f.url)} "
            "and are only filled in by JavaScript, so crawlers and search snippets see zeros",
            f.url, len(hit))
    hit = pages_with("stray_comment")
    if hit:
        add("stray_comment", "low",
            f"A broken HTML comment renders as visible text ('{hit[0].signals['stray_comment']}') on "
            f"{len(hit)} page(s)", hit[0].url, len(hit))
    email_hrefs = {x.split("'")[1] for f in facts for x in f.signals.get("broken_email_links", [])}
    hit = [f for f in pages_with("placeholder") if set(f.signals["placeholder"]) - email_hrefs]
    if hit:
        vals = sorted({v for f in hit for v in f.signals["placeholder"]} - email_hrefs)
        add("placeholder_content", "high",
            f"Placeholder content is live: {', '.join(repr(v) for v in vals[:3])} on {len(hit)} page(s)",
            hit[0].url, len(hit))
    hit = pages_with("stale_copyright")
    if hit:
        add("stale_copyright", "low",
            f"Copyright notice still says {hit[0].signals['stale_copyright']}", hit[0].url, len(hit))
    hit = pages_with("heading_misuse")
    if hit:
        add("heading_misuse", "low",
            f"Non-heading text is marked up as a heading ({hit[0].signals['heading_misuse'][0]})",
            hit[0].url, len(hit))
    if home.signals.get("layout_headings"):
        add("layout_headings", "low",
            f"The homepage uses {home.signals['layout_headings']} H5/H6 tags as styled labels "
            "instead of a real H1-H3 outline", home.url, 1)
    hit = pages_with("h1_equals_h2")
    if hit:
        add("h1_equals_h2", "low",
            f"{len(hit)} page(s) repeat the H1 as the first H2 ('{hit[0].signals['h1_equals_h2'][:60]}')",
            hit[0].url, len(hit))
    typos = list(dict.fromkeys(t for f in facts for t in f.signals.get("typos", [])))
    if typos:
        where = next(f for f in facts if f.signals.get("typos"))
        add("typos", "high", "Visible misspelling: " + "; ".join(typos[:3]) + f" on {_short(where.url)}"
            + (f" and {len(pages_with('typos')) - 1} more page(s)" if len(pages_with("typos")) > 1 else ""),
            where.url, len(pages_with("typos")))
    mism = list(dict.fromkeys(m for f in facts for m in f.signals.get("name_mismatch", [])))
    if mism:
        add("name_mismatch", "medium", "Name spelled differently in a heading and in the menu: " + "; ".join(mism[:2]),
            next(f.url for f in facts if f.signals.get("name_mismatch")), len(pages_with("name_mismatch")))
    if home.signals.get("leaky_headers"):
        leaks = home.signals["leaky_headers"]
        debug = any("debug" in h for h in leaks)
        eol = [m.group(0) for h in leaks for m in [EOL_SOFTWARE.search(h)] if m]
        add("leaky_headers", "medium" if debug or eol else "low",
            ("Response headers expose debug information: " if debug else "Response headers disclose exact software "
             "versions: ") + ", ".join(leaks[:3]) + (f" ({', '.join(eol)} is end-of-life)" if eol else ""),
            home.url, 1)
    hit = pages_with("legacy_scripts")
    if hit:
        add("legacy_scripts", "low",
            "Still loads obsolete browser shims/CDNs: " + ", ".join(hit[0].signals["legacy_scripts"]),
            hit[0].url, len(hit))
    empty = sorted({m for f in facts for m in f.signals.get("empty_meta", [])} & {"google-site-verification", "fb:app_id"})
    if empty:
        add("empty_meta", "low", "Meta tags left with empty content: " + ", ".join(empty), home.url,
            len([f for f in facts if set(f.signals.get("empty_meta", [])) & set(empty)]))

    a = tech.analytics.details or {}
    for name, cov in (a.get("coverage") or {}).items():
        if not cov["on_homepage"] and cov["pages"] >= 2:
            add("tracker_not_on_homepage", "low",
                f"{TRACKER_NAMES.get(name, name)} loads on {cov['pages']} of {len(facts)} pages but not on the homepage, "
                "so homepage traffic is not measured", home.url, cov["pages"])
    hit = pages_with("nav_headings")
    if len(hit) >= max(2, len(facts) // 2):
        labels = list(dict.fromkeys(x.split(": ", 1)[-1] for f in hit for x in f.signals["nav_headings"]))
        first = next((h for h in (home.signals.get("nav_headings") or [])), None)
        add("nav_headings", "low",
            f"Template labels are marked up as headings on {len(hit)} of {len(facts)} pages "
            f"({', '.join(repr(x) for x in labels[:5])}), usually visually hidden block titles"
            + (f"; the homepage outline starts with {first.split(':')[0]} '{first.split(': ', 1)[-1]}'"
               if first and home.signals["nav_headings"][0] == first and not home.h1 else ""),
            home.url, len(hit))
    hit = pages_with("jsonld_issues")
    if hit:
        issues = Counter(i for f in hit for i in f.signals["jsonld_issues"])
        worst = sorted(issues.items(), key=lambda kv: (0 if "leaked" in kv[0] or "FAQPage" in kv[0] else 1, -kv[1]))
        add("jsonld_issues", "high" if any("leaked" in i for i in issues) else "medium",
            "Structured data defects: " + "; ".join(f"{i} ({c} page{'s' if c > 1 else ''})" for i, c in worst[:4]),
            hit[0].url, len(hit))
    if home.signals.get("zoom_blocked"):
        add("zoom_blocked", "medium",
            f"The viewport blocks pinch-zoom ('{home.signals['zoom_blocked']}'), an accessibility failure "
            "(WCAG 1.4.4) that Lighthouse flags", home.url, 1)
    hit = pages_with("malformed_numbers")
    if hit:
        add("malformed_numbers", "medium",
            f"Malformed figure in the copy: {', '.join(repr(x) for x in hit[0].signals['malformed_numbers'])} "
            f"on {_short(hit[0].url)}", hit[0].url, len(hit))
    hit = pages_with("extension_attrs")
    if hit:
        add("extension_attrs", "low",
            f"Browser-extension markup was saved into the page ({', '.join(hit[0].signals['extension_attrs'])}, "
            "e.g. Internet Download Manager / Grammarly), a sign content was pasted from a browser", hit[0].url, len(hit))
    hit = pages_with("bad_hrefs")
    if len(hit) >= 2:
        empty = sum(f.signals["bad_hrefs"]["empty"] for f in hit)
        spaced = sum(f.signals["bad_hrefs"]["whitespace"] for f in hit)
        eg = next((f.signals["bad_hrefs"]["examples"][0] for f in hit if f.signals["bad_hrefs"]["examples"]), None)
        add("bad_hrefs", "low",
            f"Links with empty href=\"\" ({empty}) or stray whitespace in the URL ({spaced})"
            + (f", e.g. {eg}" if eg else "") + f" across {len(hit)} pages", hit[0].url, len(hit))
    hit = pages_with("broken_email_links")
    if hit:
        add("broken_email_links", "high",
            f"Email links are broken on {len(hit)} page(s): {hit[0].signals['broken_email_links'][0]} has no mailto: "
            "and clicking it opens a 404", hit[0].url, len(hit))
    hit = pages_with("malformed_meta")
    if hit:
        add("malformed_meta", "low", "Malformed meta tag names: " + ", ".join(hit[0].signals["malformed_meta"]),
            hit[0].url, len(hit))
    a0 = tech.analytics.details or {}
    if a0.get("gtm_environment_snippet"):
        add("gtm_environment", "medium",
            f"The page loads {a0.get('gtm_container')} with an environment snippet (gtm_auth / gtm_preview=env-…) "
            "instead of the live container snippet", home.url, 1)
    if a0.get("dead_universal_analytics"):
        add("dead_universal_analytics", "low",
            f"Universal Analytics IDs still configured ({', '.join(a0['dead_universal_analytics'][:3])}"
            + (f", referenced {crawl.tag_manager.ua_references}x in {a0.get('gtm_container')}"
               if crawl.tag_manager and crawl.tag_manager.ua_references else "")
            + "); UA stopped processing data in 2023, so these tags do nothing", home.url, 0)
    live = [d for d in crawl.duplicate_slugs if d.status_a == 200 and d.status_b == 200]
    if live:
        same = [d for d in live if d.title_a and d.title_a == d.title_b]
        pairs = "; ".join(f"{d.path_a} and {d.path_b}" for d in live[:3])
        self_canon = all(d.canonical_a and d.canonical_b and d.canonical_a != d.canonical_b for d in live)
        add("duplicate_slugs", "high" if same else "medium",
            f"Duplicate pages under CMS counter slugs: {pairs} return 200"
            + (" with identical titles" if len(same) == len(live) else "")
            + (" and each canonicalises to itself, so they compete for the same queries" if self_canon else ""),
            live[0].path_b, 2 * len(live))
    if a.get("ids") and not a.get("google_analytics") and ("gtm" not in a["ids"] or a.get("gtm_container_read")):
        found = sorted(set(a["ids"]) | set(a.get("via_gtm") or {}))
        add("no_google_analytics", "low",
            "No Google Analytics 4 found in the pages or the GTM container; only "
            + ", ".join(TRACKER_NAMES.get(k, k) for k in found), home.url, 0)
    return out
