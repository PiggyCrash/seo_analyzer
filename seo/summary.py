"""Compact, human-readable report (the format saved to output/sites/ and --out).

The full SiteReport (every check, crawl detail and page) stays in output/history/.
This module turns it into a short summary: one sentence per check, keyword lists
as plain strings, and quick wins as a list of actions.
"""
from __future__ import annotations

from typing import Any, Optional

NOT_VERIFIED = "Not verified"
TRACKER_NAMES = {
    "ga4": "Google Analytics 4", "universal_analytics": "Universal Analytics", "gtm": "GTM",
    "meta_pixel": "Meta Pixel", "tiktok_pixel": "TikTok Pixel", "linkedin_insight": "LinkedIn Insight",
    "hotjar": "Hotjar", "microsoft_clarity": "Microsoft Clarity", "plausible": "Plausible",
    "google_ads": "Google Ads", "x_pixel": "X Pixel",
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _not_verified(c: dict) -> Optional[str]:
    if c["status"] == "not_checked":
        return f"{NOT_VERIFIED} ({c['reason']})" if c.get("reason") else NOT_VERIFIED
    if c["status"] == "error":
        return f"Check failed ({c.get('note') or c.get('reason') or 'error'})"
    return None


def _text(c: dict, label: str) -> str:
    d, n = c["details"], c.get("pages_checked") or 0
    if c["status"] == "missing":
        return f"MISSING on all {_plural(n, 'page')} checked"
    home = f"Homepage: '{c['value']}' ({d['homepage_length']} chars)" if c.get("value") else "MISSING on homepage"
    issues = [f"{d[k]} {k.replace('_', ' ')}" for k in ("missing", "too_long", "too_short", "duplicates") if d.get(k)]
    if not issues:
        return f"{home}; all {_plural(n, 'page')} OK"
    return f"{home}; issues across {n} pages: {', '.join(issues)} (recommended {d['recommended_length']} chars)"


def _og(c: dict) -> str:
    if c["status"] == "missing":
        return "MISSING (no og:title / og:description) — social shares fall back to defaults"
    tags = ", ".join(c.get("value") or [])
    if c["status"] == "partial":
        extra = [] if c["details"].get("homepage_og_image") else ["no og:image on homepage"]
        if not tags:
            return "Partial — " + "; ".join(["no og tags on homepage"] + extra[1:] + (
                [f"incomplete on {_plural(c['pages_affected'], 'page')}"] if c.get("pages_affected") else []))
        if c.get("pages_affected"):
            extra.append(f"incomplete on {_plural(c['pages_affected'], 'page')}")
        return f"Partial ({tags}) — " + "; ".join(extra)
    return f"Present ({tags})"


def _h1(c: dict) -> str:
    d, h1 = c["details"], c.get("value") or []
    home = f"Homepage H1: '{h1[0]}'" if len(h1) == 1 else (
        f"Homepage has {len(h1)} H1s" if h1 else "No H1 on homepage")
    issues = []
    if d.get("missing"):
        issues.append(f"{d['missing']} page(s) without H1")
    if d.get("multiple"):
        issues.append(f"{d['multiple']} page(s) with several H1s")
    return home + (" — " + ", ".join(issues) if issues else " — one H1 per page")


def _headings(c: dict, home_h2: list[str]) -> str:
    h2 = f"Homepage H2s: {' → '.join(home_h2[:6])}" + (" …" if len(home_h2) > 6 else "") if home_h2 else "No H2s on homepage"
    if c["status"] == "partial" and c.get("pages_affected"):
        return f"{h2}. Skipped heading levels on {_plural(c['pages_affected'], 'page')}"
    return f"{h2}. Hierarchy OK"


def _alt(c: dict) -> str:
    d = c["details"]
    total = d.get("images_total", 0)
    if not total:
        return "No <img> tags found"
    missing = d.get("images_missing_alt", 0)
    if not missing:
        return f"All {total} images have alt text"
    return f"{missing} of {total} images missing alt text (on {_plural(c.get('pages_affected') or 0, 'page')})"


def _schema(c: dict) -> str:
    d = c["details"]
    if c["status"] == "missing":
        return "NONE — no Organization, WebSite or Article JSON-LD found — a significant gap"
    types = ", ".join(d.get("types_found", {})) or "none"
    gaps = [name for key, name in (("has_organization", "Organization"), ("has_website", "WebSite"))
            if not d.get(key)]
    if d.get("blog_posts_checked") and d.get("blog_posts_with_article_schema", 0) < d["blog_posts_checked"]:
        gaps.append("Article schema on some blog posts")
    where = "" if c["status"] == "present" else " (inner pages only, not on homepage)"
    return f"Found: {types}{where}" + (f" — missing {', '.join(gaps)}" if gaps else "")


def _canonical(c: dict) -> str:
    d, n = c["details"], c.get("pages_checked") or 0
    if c["status"] == "missing":
        return "MISSING on all pages"
    parts = []
    if d.get("missing"):
        parts.append(f"missing on {d['missing']} of {n} pages")
    if d.get("non_self_referencing"):
        parts.append(f"{d['non_self_referencing']} not self-referencing")
    if d.get("cross_domain"):
        parts.append(f"{d['cross_domain']} point to another domain")
    return "Present" + (" — " + ", ".join(parts) if parts else " and self-referencing on all pages")


def _sitemap(c: dict) -> str:
    d = c["details"]
    if c["status"] == "missing":
        return "MISSING — no XML sitemap found"
    if c["status"] == "partial":
        return f"Invalid — {c.get('value')} is not a valid XML sitemap"
    parts = [f"sitemap index with {_plural(d.get('child_sitemaps') or 0, 'child sitemap')}"] if d.get("is_index") else []
    if d.get("url_count") is not None:
        parts.append(_plural(d["url_count"], "URL"))
    robots = "declared in robots.txt" if d.get("declared_in_robots") else "not declared in robots.txt"
    return "Present" + (f" ({', '.join(parts)})" if parts else "") + f" — {robots}"


def _robots(c: dict) -> str:
    if c["status"] == "missing":
        return "MISSING (404) — crawlers assume allow-all, but no sitemap reference"
    if c["status"] == "partial":
        return "Present but blocks crawlers from the homepage"
    d = c["details"]
    return "Present" + (" — references the sitemap" if d.get("sitemaps") else " — no sitemap reference")


def _analytics(c: dict) -> str:
    if c["status"] == "missing":
        return "None detected"
    parts = []
    for key, ids in c["details"].items():
        name = TRACKER_NAMES.get(key, key.replace("_", " ").title())
        parts.append(f"{name} ({', '.join(ids)})" if ids else name)
    return " + ".join(parts)


def _ssl(c: dict) -> str:
    d = c["details"]
    if c["status"] == "missing":
        return "No — " + (c.get("note") or "served over plain HTTP")
    extra = []
    if d.get("http_redirects_to_https") is False:
        extra.append("HTTP does not redirect to HTTPS")
    if d.get("hsts"):
        extra.append("HSTS enabled")
    return "Yes" + (" — " + ", ".join(extra) if extra else "")


def _mobile(c: dict) -> str:
    if c["status"] == "present":
        return "Responsive viewport meta tag present"
    if c["status"] == "partial":
        return f"Viewport tag present but not responsive ('{c.get('value')}')"
    return "MISSING viewport meta tag — likely not mobile-friendly"


def _platform(c: dict) -> str:
    if c.get("value"):
        return c["value"]
    gen = (c.get("details") or {}).get("generator")
    return f"Unknown (generator: {gen})" if gen else "Not detected"


def _cookie(c: dict) -> str:
    d = c["details"]
    if c["status"] == "present":
        return f"Present ({c.get('value')})"
    if d.get("tracking_without_detected_consent"):
        return ("Not detected — tracking loads without consent, potential PDPA / GDPR compliance gap "
                "(banners injected via JavaScript may be missed)")
    return "Not detected (no ad/analytics trackers either)"


def _indexability(c: dict) -> str:
    d = c["details"]
    if c["status"] == "missing":
        return "BLOCKED — " + ("homepage is noindex" if d.get("homepage_noindex") else "robots.txt blocks Googlebot")
    if c["status"] == "partial":
        return f"Indexable, but {_plural(c.get('pages_affected') or 0, 'page')} marked noindex"
    return "Indexable (no noindex, Googlebot allowed)"


FORMATTERS = (
    ("meta_titles", "meta_title", lambda c, _: _text(c, "title")),
    ("meta_descriptions", "meta_description", lambda c, _: _text(c, "meta description")),
    ("og_tags", "open_graph", lambda c, _: _og(c)),
    ("h1_tags", "h1", lambda c, _: _h1(c)),
    ("h2_h3_hierarchy", "heading_hierarchy", _headings),
    ("image_alt_text", "image_alt_text", lambda c, _: _alt(c)),
    ("schema_markup", "schema_markup", lambda c, _: _schema(c)),
    ("canonical_urls", "canonical", lambda c, _: _canonical(c)),
    ("indexability", "indexability", lambda c, _: _indexability(c)),
    ("sitemap", "sitemap", lambda c, _: _sitemap(c)),
    ("robots_txt", "robots_txt", lambda c, _: _robots(c)),
    ("analytics", "analytics", lambda c, _: _analytics(c)),
    ("ssl", "https", lambda c, _: _ssl(c)),
    ("mobile", "mobile_viewport", lambda c, _: _mobile(c)),
    ("platform", "platform", lambda c, _: _platform(c)),
    ("cookie_banner", "cookie_banner", lambda c, _: _cookie(c)),
)


def _home_h2(report: dict) -> list[str]:
    for page in report.get("pages") or []:
        facts = page.get("facts")
        if facts and facts.get("page_type") == "home":
            return facts.get("h2") or []
    return []


def _rating(report: dict) -> str:
    score, crawl = report["score"], report["crawl"]
    if score["value"] is None:
        why = crawl.get("reason") or crawl["status"]
        return f"{score['label']} (crawl {crawl['status']}: {why})"
    rating = f"{score['label']} ({score['value']}/100)"
    summary = report["llm_analysis"].get("summary")
    return f"{rating} — {summary}" if summary else rating


def _blog(report: dict) -> str:
    cs = report["content_stats"]
    if cs["blog_detected"]:
        sitemap = f", {_plural(cs['sitemap_blog_urls'], 'blog URL')} in sitemap" if cs.get("sitemap_blog_urls") else ""
        blog = f"Present ({_plural(cs['blog_posts_crawled'], 'post')} crawled{sitemap})"
    else:
        blog = "Not detected"
    assessment = report["llm_analysis"].get("content_assessment")
    return f"{blog} — {assessment}" if assessment else blog


def _llm_missing(llm: dict) -> str:
    return f"Not analyzed (LLM {llm['status']}" + (f": {llm['reason']})" if llm.get("reason") else ")")


def summarize(report: dict) -> dict[str, Any]:
    """Full report dict (SiteReport.model_dump(mode='json')) -> compact summary dict."""
    tech, llm = report["technical_seo"], report["llm_analysis"]
    home_h2 = _home_h2(report)
    technical = {}
    for out_key, check_key, fmt in FORMATTERS:
        c = tech[check_key]
        technical[out_key] = _not_verified(c) or fmt(c, home_h2)

    llm_ok = llm["status"] == "ok"
    cs = report["content_stats"]
    return {
        "site": report["site"],
        "company": report.get("company"),
        "analysis_type": "SEO Analysis",
        "sector": report.get("sector"),
        "analyzed_at": report["analyzed_at"],
        "overall_rating": _rating(report),
        "technical_seo": technical,
        "content_seo": {
            "keyword_strategy": llm.get("keyword_strategy") if llm_ok else _llm_missing(llm),
            "captured_keywords": llm.get("captured_keywords") or [],
            "missed_keywords": [k["keyword"] for k in llm.get("missed_keywords") or []],
            "pages": (f"{cs['pages_analyzed']} pages analyzed, avg {cs['avg_word_count']} words"
                      + (f", {cs['thin_pages']} thin (<300 words)" if cs.get("thin_pages") else "")),
            "blog": _blog(report),
        },
        "competitive_seo_position": llm.get("competitive_position") if llm_ok else _llm_missing(llm),
        "quick_wins": [q["action"] for q in report["quick_wins"]],
    }
