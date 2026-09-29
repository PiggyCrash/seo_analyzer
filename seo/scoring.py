"""Deterministic score + rule-based quick wins. Reproducible: same facts -> same score."""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlparse

from .extractor import TRACKER_NAMES
from .schema import Check, ContentStats, Finding, QuickWin, Score, ScoreItem, TechnicalSEO

WEIGHTS = {
    "indexability": 12, "https": 10, "meta_title": 10, "meta_description": 10,
    "schema_markup": 10, "sitemap": 8, "h1": 7, "canonical": 7, "open_graph": 6,
    "image_alt_text": 6, "not_found_handling": 6, "robots_txt": 5, "mobile_viewport": 5, "broken_links": 5,
    "heading_hierarchy": 2, "lang": 2,
}
EARN = {"present": 1.0, "partial": 0.5, "missing": 0.0, "error": 0.0}
LABELS = ((85, "Excellent"), (70, "Good"), (50, "Fair"), (30, "Weak"), (0, "Poor"))
MIN_COVERAGE = 0.5
CONFIDENCE_PAGES = ((10, "high"), (5, "medium"), (1, "low"))


def compute_score(tech: TechnicalSEO, pages_scored: int = 0) -> Score:
    breakdown, available, earned = {}, 0.0, 0.0
    for name, weight in WEIGHTS.items():
        check: Check = getattr(tech, name)
        if check.status == "not_checked":
            breakdown[name] = ScoreItem(weight=weight, earned=0, status="not_checked")
            continue
        got = weight * EARN[check.status]
        available += weight
        earned += got
        breakdown[name] = ScoreItem(weight=weight, earned=got, status=check.status)
    coverage = round(available / sum(WEIGHTS.values()), 2)
    if coverage < MIN_COVERAGE:
        return Score(value=None, label="Insufficient data", coverage=coverage, pages_scored=pages_scored,
                     breakdown=breakdown)
    value = round(100 * earned / available)
    label = next(l for threshold, l in LABELS if value >= threshold)
    confidence = next((c for threshold, c in CONFIDENCE_PAGES if pages_scored >= threshold), "none")
    caveats = ["Technical-hygiene score only: content quality, rankings and backlinks are not measured"]
    if confidence in ("low", "none"):
        caveats.insert(0, f"Based on only {pages_scored} page(s); site-wide conclusions are unreliable")
    missing_core = [n for n in ("schema_markup", "h1", "robots_txt", "meta_title", "meta_description")
                    if breakdown[n].status == "missing"]
    if missing_core:
        caveats.append("Missing entirely: " + ", ".join(missing_core))
    return Score(value=value, label=label, coverage=coverage, pages_scored=pages_scored,
                 confidence=confidence, caveats=caveats, breakdown=breakdown)


def _qw(action, priority, effort, category, check, rationale=None, evidence=None) -> QuickWin:
    return QuickWin(action=action, priority=priority, effort=effort, category=category,
                    source="rule", related_check=check, rationale=rationale, evidence=evidence)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _path(url: Optional[str]) -> str:
    return (urlparse(url).path or "/") if url else ""


def _eg(urls: list[str], n: int = 2) -> str:
    urls = [_path(u) for u in urls[:n] if u]
    return f" (e.g. {', '.join(urls)})" if urls else ""


def _title_win(c: Check, n: int) -> Optional[QuickWin]:
    d = c.details
    if c.status == "missing":
        return _qw("Add unique <title> tags (30-60 characters) to every page", "high", "low", "technical", "meta_title")
    if c.status != "partial":
        return None
    parts = []
    if d.get("placeholder"):
        val = (d.get("placeholder_values") or [""])[0]
        parts.append(f"{d['placeholder']} page(s) have the bare title '{val}'{_eg(d.get('examples_placeholder', []))}")
    for g in (d.get("duplicate_groups") or [])[:2]:
        if not d.get("placeholder") or g["value"] not in (d.get("placeholder_values") or []):
            parts.append(f"'{g['value']}' is reused on {g['pages']} pages")
    if d.get("examples_too_long_values"):
        v = d["examples_too_long_values"][0]
        parts.append(f"'{v[:60]}…' is {len(v)} characters")
    if d.get("missing"):
        parts.append(f"{d['missing']} page(s) have none")
    if not parts and d.get("too_short"):
        parts.append(f"{d['too_short']} of {n} titles are under 30 characters")
    return _qw("Rewrite <title> tags: " + "; ".join(parts), "high" if d.get("missing") or d.get("placeholder") else "medium",
               "low", "technical", "meta_title")


def _desc_win(c: Check, n: int) -> Optional[QuickWin]:
    d = c.details
    if c.status == "missing":
        return _qw("Add a unique meta description (70-160 characters) to every page", "high", "low",
                   "technical", "meta_description")
    if c.status != "partial":
        return None
    parts = []
    groups = d.get("duplicate_groups") or []
    if groups and groups[0]["pages"] >= max(3, n // 2):
        g = groups[0]
        parts.append(f"one sentence ('{g['value'][:50]}…') is hard-coded on {g['pages']} of {n} pages; "
                     "make it page-specific in the shared template")
    elif d.get("duplicates"):
        parts.append(f"{d['duplicates']} pages share a description")
    if d.get("placeholder"):
        parts.append(f"the description is '{(d.get('placeholder_values') or ['-'])[0]}'"
                     f"{_eg(d.get('examples_placeholder', []), 1)}")
    if d.get("missing"):
        parts.append(f"{d['missing']} page(s) have none{_eg(d.get('examples_missing', []))}")
    if not parts:
        parts.append(", ".join(f"{d[k]} {k.replace('_', ' ')}" for k in ("too_long", "too_short") if d.get(k)))
    return _qw("Fix meta descriptions: " + "; ".join(parts), "high" if len(groups) and groups[0]["pages"] >= n // 2 else "medium",
               "low", "technical", "meta_description")


def _sitemap_win(t: TechnicalSEO) -> list[QuickWin]:
    s, b, r = t.sitemap, t.broken_links, t.robots_txt
    sd, bd = s.details or {}, b.details or {}
    robots_missing = r.status == "missing"
    robots_tail = f"; then add a robots.txt containing 'Sitemap: {s.value}'" if robots_missing and s.value else ""
    if s.status == "missing":
        return [_qw("Publish an XML sitemap, reference it in robots.txt and submit it in Google Search Console",
                    "high", "low", "technical", "sitemap")]
    if s.status == "partial":
        return [_qw("Fix the sitemap: the file exists but is not a valid XML sitemap" + robots_tail,
                    "high", "low", "technical", "sitemap")]
    if s.status != "present":
        return []
    parts = []
    fixes = bd.get("prefix_fixes") or sd.get("prefix_fixes") or {}
    if fixes:
        dead = ", ".join(f"/{k}/" for k in fixes)
        live = ", ".join(sorted({f"/{v}/" for v in fixes.values()}))
        count = bd.get("sitemap_urls_with_dead_prefix") or 0
        total = sd.get("url_count") or 0
        eg = (sd.get("prefix_fix_examples") or [""])[0]
        eg = f" (e.g. {_path(eg.split(' -> ')[0])} 404s, {_path(eg.split(' -> ')[-1])} works)" if eg else ""
        parts.append(f"{count} of {total} entries use {dead} which return 404, while the same pages load under "
                     f"{live}{eg}")
    elif bd.get("in_sitemap"):
        parts.append(f"{bd['in_sitemap']} of {bd['sitemap_urls_fetched']} sitemap URLs fetched return 404"
                     f"{_eg(bd.get('examples_sitemap', []), 1)}")
    if sd.get("malformed_entries"):
        eg = (sd.get("malformed_examples") or [""])[0]
        parts.append(f"remove {sd['malformed_entries']} malformed entries (e.g. '{eg}')")
    if sd.get("duplicate_entries"):
        parts.append(f"drop {sd['duplicate_entries']} duplicate entries")
    if parts:
        return [_qw("Regenerate sitemap.xml: " + "; ".join(parts) + robots_tail,
                    "high", "low", "technical", "sitemap",
                    "Dead and malformed sitemap URLs waste crawl budget and keep real pages undiscovered")]
    if robots_missing:
        return [_qw(f"Add a robots.txt containing 'Sitemap: {s.value}'", "low", "low", "technical", "robots_txt")]
    if not sd.get("declared_in_robots"):
        return [_qw(f"Reference the sitemap in robots.txt ('Sitemap: {s.value}')", "low", "low", "technical", "sitemap")]
    return []


def rule_quick_wins(tech: TechnicalSEO, content: ContentStats,
                    findings: Iterable[Finding] = ()) -> list[QuickWin]:
    w: list[QuickWin] = []
    t = tech
    n = t.meta_title.pages_checked or content.pages_analyzed
    fx = {f.type: f for f in findings}

    if t.indexability.status == "missing":
        w.append(_qw("Remove the noindex directive / robots.txt rule that stops Google indexing the homepage",
                     "high", "low", "technical", "indexability", "Nothing else matters if the site cannot be indexed"))
    elif t.indexability.status == "partial":
        w.append(_qw(f"Review {t.indexability.pages_affected} page(s) marked noindex and confirm each is intentional",
                     "medium", "low", "technical", "indexability"))

    if t.https.status == "missing":
        w.append(_qw("Serve the whole site over HTTPS with a valid certificate", "high", "medium", "technical", "https"))
    elif t.https.status == "partial":
        w.append(_qw("301-redirect all HTTP URLs to HTTPS", "high", "low", "technical", "https"))

    nf = t.not_found_handling
    if nf.status == "missing":
        probe = next((p for p in reversed(nf.details.get("probes", [])) if p.get("status_code") == 200), {})
        shells = nf.details.get("examples_empty_template") or []
        tail = (f"; {nf.details['empty_template_pages']} sampled pages are that empty shell{_eg(shells, 3)}, "
                "301 them to the real page or remove them") if shells else ""
        w.append(_qw(f"Return HTTP 404 for unknown URLs: {_path(probe.get('url'))} answers 200 with the "
                     f"empty template (title '{probe.get('title') or ''}'){tail}",
                     "high", "medium", "technical", "not_found_handling",
                     "Every mistyped or stale link becomes an indexable empty page"))
    elif nf.status == "partial":
        w.append(_qw(f"Redirect or remove {nf.pages_affected} empty template page(s){_eg(nf.details.get('examples_empty_template', []), 3)}",
                     "medium", "low", "technical", "not_found_handling"))

    w.extend(q for q in (_title_win(t.meta_title, n), _desc_win(t.meta_description, n)) if q)

    if t.schema_markup.status == "missing":
        blog = " and BlogPosting/NewsArticle schema to posts" if content.blog_detected else ""
        w.append(_qw(f"Add Organization + WebSite JSON-LD to the homepage{blog}",
                     "high", "low", "technical", "schema_markup", "Enables rich results and entity understanding"))
    elif t.schema_markup.status == "partial":
        w.append(_qw("Add structured data to the homepage (it currently exists only on inner pages)",
                     "medium", "low", "technical", "schema_markup"))
    else:
        d = t.schema_markup.details
        if d.get("blog_posts_checked") and d.get("blog_posts_with_article_schema", 0) < d["blog_posts_checked"]:
            w.append(_qw("Add BlogPosting/Article schema to blog posts", "medium", "low", "technical", "schema_markup"))

    w.extend(_sitemap_win(t))
    if t.robots_txt.status == "partial":
        w.append(_qw("Stop robots.txt from blocking crawlers on the homepage", "high", "low", "technical", "robots_txt"))

    if t.canonical.status in ("missing", "partial"):
        w.append(_qw(f"Add self-referencing canonical tags to {t.canonical.pages_affected} page(s)"
                     f"{_eg(t.canonical.details.get('examples_missing', []))}", "medium", "low", "technical", "canonical"))

    og = t.open_graph
    if og.status in ("missing", "partial"):
        d, parts = og.details, []
        if d.get("og_url_not_self"):
            parts.append(f"og:url is '{d.get('og_url_value')}' on {d['og_url_not_self']} inner pages, "
                         "so every share links to the homepage")
        if d.get("constant_og_title"):
            parts.append(f"og:title is always '{d['constant_og_title']}'")
        if d.get("og_site_name_is_url"):
            parts.append("og:site_name holds a URL instead of the brand name")
        if parts and og.status == "partial":
            if not d.get("homepage_og_image"):
                parts.append("the homepage has no og:image")
            w.append(_qw("Make Open Graph tags page-specific in the shared template: " + "; ".join(parts),
                         "medium", "low", "social", "open_graph", "Controls how links look when shared"))
        else:
            missing = d.get("examples_missing") or []
            where = f" (missing or incomplete on {_path(missing[0])}" + (f" and {len(missing) - 1} more" if len(missing) > 1 else "") + ")" if missing else ""
            if not missing and not d.get("homepage_og_image"):
                where = " (the homepage has no og:image)"
            w.append(_qw(f"Add Open Graph tags (og:title, og:description, og:image, og:url) to every page{where}",
                         "medium", "low", "social", "open_graph", "Controls how links look when shared"))

    h = t.h1
    if h.status in ("missing", "partial"):
        d, parts = h.details, []
        home_missing = not h.value
        if home_missing:
            top = d.get("homepage_top_heading")
            parts.append("add an H1 to the homepage" + (f" (its top heading is {top.split(':', 1)[0]} "
                                                        f"'{top.split(': ', 1)[-1][:50]}')" if top else ""))
        others = d.get("missing", 0) - (1 if home_missing else 0)
        if d.get("empty_tag"):
            parts.append(f"fill the empty <h1></h1> on {d['empty_tag']} page(s){_eg(d.get('examples_empty', []))}")
            others -= d["empty_tag"]
        if others > 0:
            parts.append(f"add an H1 to {others} more page(s)")
        if d.get("multiple"):
            parts.append(f"keep one H1 on {d['multiple']} page(s) that have several")
        w.append(_qw(_cap("; ".join(parts) or "Give every page exactly one H1"), "medium", "low", "content", "h1"))

    alt = t.image_alt_text
    if alt.status in ("missing", "partial"):
        d, parts = alt.details, []
        tpl = d.get("template_images") or []
        for img in tpl[:2]:
            parts.append(f"add alt text to {img['src']} in the shared template (missing on {img['pages']} of "
                         f"{alt.pages_checked} pages)")
        rest = d.get("images_missing_alt", 0) - sum(i["pages"] for i in tpl)
        if rest > 0:
            others = d.get("other_missing_srcs") or []
            eg = f" (e.g. {', '.join(others[:2])})" if others else ""
            parts.append(f"{rest} other image(s){eg}" if tpl else f"add alt text to {rest} image(s){eg}")
        w.append(_qw(_cap("; ".join(parts) or f"Add alt text to {d.get('images_missing_alt')} image(s)"),
                     "medium", "low", "technical", "image_alt_text"))

    if t.mobile_viewport.status != "present" and t.mobile_viewport.status != "not_checked":
        w.append(_qw("Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
                     "high", "low", "technical", "mobile_viewport"))

    hl_details = t.hreflang.details or {}
    hreflang = hl_details.get("multilingual") and not t.hreflang.value
    hl = hl_details.get("locale_prefixes") or []
    hl_tail = (" and add hreflang pairs between " + " and ".join(f"/{p}/" for p in hl[:3])) if hreflang else ""
    if t.lang.status == "missing":
        w.append(_qw("Declare the page language with <html lang=\"...\">" + hl_tail, "low", "low", "technical", "lang"))
    elif t.lang.status == "partial":
        d = t.lang.details
        w.append(_qw(f"Set <html lang> from the page's language: {t.lang.pages_affected} page(s) declare "
                     f"'{t.lang.value}' but the content is '{d.get('content_language')}'" + hl_tail,
                     "medium" if d.get("homepage_mismatch") or hreflang else "low", "low", "technical", "lang",
                     "Search engines use lang/hreflang to serve pages to the right-language audience"))
    elif hreflang:
        w.append(_qw("Add hreflang pairs between " + " and ".join(f"/{p}/" for p in hl[:3]) + " versions",
                     "medium", "low", "technical", "hreflang"))

    b = t.broken_links
    if b.status == "partial" and b.details.get("linked_from_homepage"):
        d = b.details
        w.append(_qw(f"Fix {d['linked_from_homepage']} homepage link(s) that return 404 "
                     f"(e.g. {d['examples_linked'][0]})", "high", "low", "technical", "broken_links"))

    layout = fx.pop("layout_headings", None)
    if layout:
        count = re.search(r"\d+", layout.message)
        w.append(_qw(f"Restyle the {count.group(0) if count else ''} H5/H6 layout labels on the homepage as "
                     "<p>/<div> and keep heading tags for a real H1-H3 outline", "low", "low", "content",
                     "heading_hierarchy"))
    elif t.heading_hierarchy.status == "partial":
        w.append(_qw(f"Fix skipped heading levels on {t.heading_hierarchy.pages_affected} page(s)",
                     "low", "low", "content", "heading_hierarchy"))

    if t.cookie_banner.details.get("tracking_without_detected_consent"):
        loaded = ", ".join(TRACKER_NAMES.get(x, x) for x in t.cookie_banner.details.get("trackers_loaded", []))
        w.append(_qw(f"Add a consent banner before {loaded} loads ({t.cookie_banner.details.get('privacy_law')} exposure)",
                     "high", "medium", "compliance", "cookie_banner", "No consent mechanism detected; verify manually"))

    w.extend(_finding_wins(fx))
    if content.thin_pages:
        w.append(_qw(f"Expand {content.thin_pages} thin page(s) under 300 words of own content",
                     "medium", "medium", "content", "thin_content"))
    return w


def _finding_wins(fx: dict[str, Finding]) -> list[QuickWin]:
    """One concrete fix per site-specific finding."""
    w = []

    def add(type_, action, priority="low", effort="low", category="technical"):
        f = fx.get(type_)
        if f:
            w.append(_qw(action(f), priority, effort, category, type_, evidence=f.evidence))

    add("placeholder_content", lambda f: "Remove the placeholder content that is live in production: "
        + f.message.split(": ", 1)[-1], "high", "low", "content")
    add("zero_counters", lambda f: "Server-render the homepage statistics: " + f.message[0].lower() + f.message[1:],
        "medium", "low", "content")
    add("name_mismatch", lambda f: "Fix the inconsistent spelling: " + f.message.split(": ", 1)[-1],
        "medium", "low", "content")
    leak = fx.get("leaky_headers")
    add("leaky_headers", lambda f: "Remove the leaking response header(s): " + f.message.split(": ", 1)[-1],
        "medium" if leak and "debug" in leak.message.split(": ", 1)[-1] else "low", "low", "technical")
    add("no_google_analytics", lambda f: "Install GA4 (or GTM) so organic traffic can be measured: "
        + f.message[0].lower() + f.message[1:], "medium", "low", "technical")
    add("stray_comment", lambda f: "Remove the broken HTML comment that " + f.message.split("comment ", 1)[-1],
        "low", "low", "content")
    add("stale_copyright", lambda f: "Update the copyright year (" + f.message.rsplit(" ", 1)[-1] + ")",
        "low", "low", "content")
    add("heading_misuse", lambda f: "Take non-heading text out of heading tags: " + f.message.split("(", 1)[-1].rstrip(")"),
        "low", "low", "content")
    add("h1_equals_h2", lambda f: "Drop the H2 that repeats the H1 on " + f.message.split(" repeat", 1)[0],
        "low", "low", "content")
    add("legacy_scripts", lambda f: "Drop obsolete scripts: " + f.message.split(": ", 1)[-1], "low", "low", "performance")
    add("empty_meta", lambda f: "Fill in or remove empty meta tags: " + f.message.split(": ", 1)[-1])
    add("tracker_not_on_homepage", lambda f: "Load tracking consistently: " + f.message)
    return w


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2}


def sort_wins(wins: list[QuickWin]) -> list[QuickWin]:
    return sorted(wins, key=lambda q: (PRIORITY_ORDER[q.priority], EFFORT_ORDER[q.effort], q.source != "rule"))
