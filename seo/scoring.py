"""Deterministic score + rule-based quick wins. Reproducible: same facts -> same score."""
from __future__ import annotations

from .schema import Check, ContentStats, QuickWin, Score, ScoreItem, TechnicalSEO

WEIGHTS = {
    "indexability": 12, "https": 10, "meta_title": 10, "meta_description": 10,
    "schema_markup": 10, "sitemap": 8, "h1": 7, "canonical": 7, "open_graph": 6,
    "image_alt_text": 6, "robots_txt": 5, "mobile_viewport": 5, "heading_hierarchy": 2, "lang": 2,
}
EARN = {"present": 1.0, "partial": 0.5, "missing": 0.0, "error": 0.0}
LABELS = ((85, "Excellent"), (70, "Good"), (50, "Fair"), (30, "Weak"), (0, "Poor"))
MIN_COVERAGE = 0.5


def compute_score(tech: TechnicalSEO) -> Score:
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
        return Score(value=None, label="Insufficient data", coverage=coverage, breakdown=breakdown)
    value = round(100 * earned / available)
    label = next(l for threshold, l in LABELS if value >= threshold)
    return Score(value=value, label=label, coverage=coverage, breakdown=breakdown)


def _qw(action, priority, effort, category, check, rationale=None) -> QuickWin:
    return QuickWin(action=action, priority=priority, effort=effort, category=category,
                    source="rule", related_check=check, rationale=rationale)


def rule_quick_wins(tech: TechnicalSEO, content: ContentStats) -> list[QuickWin]:
    w: list[QuickWin] = []
    t = tech

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

    for name, label, rng in (("meta_title", "<title> tags", "30-60"), ("meta_description", "meta descriptions", "70-160")):
        c: Check = getattr(t, name)
        d = c.details
        if c.status == "missing":
            w.append(_qw(f"Add unique {label} ({rng} characters) to every page", "high", "low", "technical", name))
        elif c.status == "partial":
            parts = [f"{d[k]} {k.replace('_', ' ')}" for k in ("missing", "too_long", "too_short", "duplicates") if d.get(k)]
            w.append(_qw(f"Fix {label}: " + ", ".join(parts), "high" if d.get("missing") else "medium",
                         "low", "technical", name))

    if t.schema_markup.status == "missing":
        w.append(_qw("Add Organization + WebSite JSON-LD to the homepage and BlogPosting/Article schema to blog posts",
                     "high", "low", "technical", "schema_markup", "Enables rich results and entity understanding"))
    elif t.schema_markup.status == "partial":
        w.append(_qw("Add structured data to the homepage (it currently exists only on inner pages)",
                     "medium", "low", "technical", "schema_markup"))
    else:
        d = t.schema_markup.details
        if d.get("blog_posts_checked") and d.get("blog_posts_with_article_schema", 0) < d["blog_posts_checked"]:
            w.append(_qw("Add BlogPosting/Article schema to blog posts", "medium", "low", "technical", "schema_markup"))

    if t.sitemap.status == "missing":
        w.append(_qw("Publish an XML sitemap, reference it in robots.txt and submit it in Google Search Console",
                     "high", "low", "technical", "sitemap"))
    elif t.sitemap.status == "partial":
        w.append(_qw("Fix the sitemap: the file exists but is not a valid XML sitemap", "high", "low", "technical", "sitemap"))
    elif t.sitemap.status == "present" and not t.sitemap.details.get("declared_in_robots") and t.robots_txt.status == "present":
        w.append(_qw("Reference the sitemap in robots.txt (Sitemap: ...)", "low", "low", "technical", "sitemap"))

    if t.robots_txt.status == "partial":
        w.append(_qw("Stop robots.txt from blocking crawlers on the homepage", "high", "low", "technical", "robots_txt"))
    elif t.robots_txt.status == "missing":
        w.append(_qw("Add a robots.txt that references the sitemap", "low", "low", "technical", "robots_txt"))

    if t.canonical.status in ("missing", "partial"):
        w.append(_qw(f"Add self-referencing canonical tags to {t.canonical.pages_affected} page(s)",
                     "medium", "low", "technical", "canonical"))
    if t.open_graph.status in ("missing", "partial"):
        w.append(_qw("Add Open Graph tags (og:title, og:description, og:image) to every page",
                     "medium", "low", "social", "open_graph", "Controls how links look when shared"))
    if t.h1.status in ("missing", "partial"):
        d = t.h1.details
        w.append(_qw(f"Give every page exactly one H1 ({d.get('missing', 0)} missing, {d.get('multiple', 0)} with several)",
                     "medium", "low", "content", "h1"))
    if t.image_alt_text.status in ("missing", "partial"):
        w.append(_qw(f"Add alt text to {t.image_alt_text.details.get('images_missing_alt')} image(s)",
                     "medium", "low", "technical", "image_alt_text"))
    if t.mobile_viewport.status != "present" and t.mobile_viewport.status != "not_checked":
        w.append(_qw("Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
                     "high", "low", "technical", "mobile_viewport"))
    if t.lang.status == "missing":
        w.append(_qw("Declare the page language with <html lang=\"...\">", "low", "low", "technical", "lang"))
    if t.heading_hierarchy.status == "partial":
        w.append(_qw(f"Fix skipped heading levels on {t.heading_hierarchy.pages_affected} page(s)",
                     "low", "low", "content", "heading_hierarchy"))
    if t.cookie_banner.details.get("tracking_without_detected_consent"):
        loaded = ", ".join(t.cookie_banner.details.get("trackers_loaded", []))
        w.append(_qw(f"Add a consent banner: the site loads tracking ({loaded}) with no consent mechanism detected",
                     "high", "medium", "compliance", "cookie_banner", "GDPR / PDPA exposure; verify manually"))
    if content.thin_pages:
        w.append(_qw(f"Expand {content.thin_pages} thin page(s) under 300 words", "medium", "medium", "content", None))
    return w


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2}


def sort_wins(wins: list[QuickWin]) -> list[QuickWin]:
    return sorted(wins, key=lambda q: (PRIORITY_ORDER[q.priority], EFFORT_ORDER[q.effort], q.source != "rule"))
