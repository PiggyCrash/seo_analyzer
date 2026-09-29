"""Compact, human-readable report (the format saved to output/sites/ and --out).

The full SiteReport (every check, crawl detail and page) stays in output/history/.
This module turns it into a short summary: one sentence per check, keyword lists
as plain strings, site-specific findings, and quick wins as plain action strings ranked by priority/effort.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

from .extractor import TRACKER_NAMES

NOT_VERIFIED = "Not verified"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _not_verified(c: dict) -> Optional[str]:
    if c["status"] == "not_checked":
        return f"{NOT_VERIFIED} ({c['reason']})" if c.get("reason") else NOT_VERIFIED
    if c["status"] == "error":
        return f"Check failed ({c.get('note') or c.get('reason') or 'error'})"
    return None


def _path(url: Optional[str]) -> str:
    return (urlparse(url).path or "/") if url else ""


def _text(c: dict, label: str) -> str:
    d, n = c["details"], c.get("pages_checked") or 0
    if c["status"] == "missing":
        return f"MISSING on all {_plural(n, 'page')} checked"
    home = f"Homepage: '{c['value']}' ({d['homepage_length']} chars)" if c.get("value") else "MISSING on homepage"
    issues = [f"{d[k]} {k.replace('_', ' ')}" for k in ("missing", "too_long", "too_short", "duplicates") if d.get(k)]
    if not issues:
        return f"{home}; all {_plural(n, 'page')} OK"
    text = f"{home}; issues across {n} pages: {', '.join(issues)} (recommended {d['recommended_length']} chars)"
    groups = d.get("duplicate_groups") or []
    if groups:
        text += ". Most reused: " + "; ".join(f"'{g['value'][:70]}' on {g['pages']} pages" for g in groups[:2])
    if d.get("placeholder"):
        vals = ", ".join(f"'{v}'" for v in d.get("placeholder_values") or [])
        what = "bare title" if label == "title" else "placeholder description"
        text += f". {d['placeholder']} page(s) have the {what} {vals}"
    return text


def _og(c: dict) -> str:
    if c["status"] == "missing":
        return "MISSING (no og:title / og:description) — social shares fall back to defaults"
    tags = ", ".join(c.get("value") or [])
    d = c["details"]
    if c["status"] == "partial":
        extra = [] if d.get("homepage_og_image") else ["no og:image on homepage"]
        if d.get("og_url_not_self"):
            extra.append(f"og:url is '{d.get('og_url_value')}' on {d['og_url_not_self']} inner pages, so shares "
                         "link to the homepage")
        if d.get("constant_og_title"):
            extra.append(f"og:title is the constant '{d['constant_og_title']}' on every page")
        if d.get("og_site_name_is_url"):
            extra.append(f"og:site_name is a URL ('{d['og_site_name_is_url']}')")
        if not tags:
            return "Partial — " + "; ".join(["no og tags on homepage"] + extra[1:] + (
                [f"incomplete on {_plural(c['pages_affected'], 'page')}"] if c.get("pages_affected") else []))
        if d.get("examples_missing"):
            extra.append(f"missing on {_plural(len(d['examples_missing']), 'page')}")
        return f"Partial ({tags}) — " + "; ".join(extra)
    return f"Present ({tags})"


def _h1(c: dict) -> str:
    d, h1 = c["details"], c.get("value") or []
    if len(h1) == 1:
        home = f"Homepage H1: '{h1[0]}'"
    elif h1:
        home = f"Homepage has {len(h1)} H1s"
    else:
        top = d.get("homepage_top_heading")
        home = "No H1 on homepage" + (f" (top heading is {top.split(':', 1)[0]} '{top.split(': ', 1)[-1][:60]}')"
                                      if top else "")
    issues = []
    if d.get("missing"):
        issues.append(f"{d['missing']} page(s) without a usable H1")
    if d.get("empty_tag"):
        eg = ", ".join(_path(u) for u in (d.get("examples_empty") or [])[:3])
        issues.append(f"{d['empty_tag']} of them render an empty <h1></h1>" + (f" ({eg})" if eg else ""))
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
    empty = d.get("images_empty_alt", 0)
    empty_txt = f"; {empty} more use alt=\"\"" if empty else ""
    if not missing:
        return f"All {total} images have an alt attribute{empty_txt}"
    text = f"{missing} of {total} images missing alt text (on {_plural(c.get('pages_affected') or 0, 'page')})"
    tpl = d.get("template_images") or []
    if tpl:
        text += " — " + "; ".join(f"{t['src']} alone accounts for {t['pages']} (one shared-template fix)" for t in tpl[:2])
    return text + empty_txt


def _schema(c: dict) -> str:
    d = c["details"]
    if c["status"] == "missing":
        return "NONE — no JSON-LD / structured data found on any crawled page — a significant gap"
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
        unique = d.get("unique_urls")
        if unique is not None and unique != d["url_count"]:
            parts.append(f"{d['url_count']} entries, {unique} unique URLs")
        else:
            parts.append(_plural(d["url_count"], "URL"))
    robots = "declared in robots.txt" if d.get("declared_in_robots") else "not declared in robots.txt"
    text = "Present" + (f" ({', '.join(parts)})" if parts else "") + f" — {robots}"
    problems = []
    fixes = d.get("prefix_fixes") or {}
    if fixes:
        counts = d.get("prefix_counts") or {}
        n = sum(counts.get(p, 0) for p in fixes)
        mapping = ", ".join(f"/{k}/ (should be /{v}/)" for k, v in fixes.items())
        problems.append(f"{n} entries use locale prefixes the site does not serve: {mapping} — they 404")
    if d.get("malformed_entries"):
        eg = (d.get("malformed_examples") or [""])[0]
        problems.append(f"{d['malformed_entries']} malformed entries (e.g. '{eg}')")
    if d.get("duplicate_entries"):
        problems.append(f"{d['duplicate_entries']} duplicate entries")
    return text + (". BROKEN: " + "; ".join(problems) if problems else "")


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
    d = c["details"]
    ids_by_tracker = d.get("ids", d) if isinstance(d.get("ids", d), dict) else {}
    coverage = d.get("coverage") or {}
    n = c.get("pages_checked")
    parts = []
    for key, ids in ids_by_tracker.items():
        name = TRACKER_NAMES.get(key, key.replace("_", " ").title())
        text = f"{name} ({', '.join(ids)})" if ids else name
        cov = coverage.get(key)
        if cov and n and not cov["on_homepage"]:
            text += f" — inner pages only ({cov['pages']} of {n}), not on the homepage"
        parts.append(text)
    text = " + ".join(parts)
    if coverage and not d.get("google_analytics"):
        text += ". No Google Analytics 4 / Tag Manager detected"
    return text


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
        law = d.get("privacy_law") or "privacy law"
        return (f"Not detected — tracking loads without consent, potential {law} compliance gap "
                "(banners injected via JavaScript may be missed)")
    return "Not detected (no ad/analytics trackers either)"


def _lang(c: dict) -> str:
    d = c["details"]
    detected = f", content detected as '{d['content_language']}'" if d.get("content_language") else ""
    if c["status"] == "missing":
        return f"MISSING <html lang>{detected}"
    if c["status"] == "partial":
        return f"MISMATCH — declared '{c.get('value')}'{detected} ({_plural(c.get('pages_affected') or 0, 'page')})"
    return f"'{c.get('value')}'{detected}"


def _broken(c: dict) -> str:
    d = c["details"]
    if c["status"] == "present":
        return f"None found in {_plural(c.get('pages_checked') or 0, 'crawled URL')}"
    if d.get("diagnosis") == "sitemap_wrong_locale_prefix":
        fixes = ", ".join(f"/{k}/ -> /{v}/" for k, v in (d.get("prefix_fixes") or {}).items())
        return (f"{d.get('in_sitemap')} of {d.get('sitemap_urls_fetched')} sitemap URLs sampled return 404, all "
                f"caused by a wrong locale prefix in the sitemap ({fixes}); no broken links found on the pages "
                "themselves")
    parts = []
    if d.get("in_sitemap"):
        parts.append(f"{d['in_sitemap']} of {d['sitemap_urls_fetched']} sitemap URLs fetched")
    if d.get("linked_from_homepage"):
        parts.append(f"{d['linked_from_homepage']} linked from the homepage")
    return f"{_plural(c.get('value') or 0, 'URL')} return 404/410 ({'; '.join(parts)}) — sample only"


def _not_found(c: dict) -> str:
    d = c["details"]
    shells = d.get("empty_template_pages") or 0
    eg = ", ".join(_path(u) for u in (d.get("examples_empty_template") or [])[:4])
    shell_txt = f"; {shells} of the {c.get('pages_checked')} sampled pages are that empty shell ({eg})" if shells else ""
    if c["status"] == "missing":
        probe = next((p for p in reversed(d.get("probes") or []) if p.get("status_code") == 200), {})
        return (f"CRITICAL — unknown URLs return HTTP 200 instead of 404 ({_path(probe.get('url'))} -> 200, "
                f"title '{probe.get('title') or ''}'), so every junk URL is an indexable empty page{shell_txt}")
    if c["status"] == "partial":
        return f"Unknown URLs return {c.get('value')}, but{shell_txt[1:]}"
    return f"OK — unknown URLs return HTTP {c.get('value')}"


def _hreflang(c: dict) -> str:
    d = c.get("details") or {}
    if c.get("value"):
        return "Present (" + ", ".join(c["value"]) + ")"
    if d.get("multilingual"):
        langs = d.get("html_lang_by_prefix") or {}
        per = "; ".join(f"/{k}/ declares lang={'/'.join(v)}" for k, v in langs.items())
        return ("MISSING — language versions " + ", ".join(f"/{p}/" for p in d.get("locale_prefixes", []))
                + " exist with no hreflang between them" + (f" ({per})" if per else ""))
    return "Not needed (single-language site)"


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
    ("soft_404", "not_found_handling", lambda c, _: _not_found(c)),
    ("sitemap", "sitemap", lambda c, _: _sitemap(c)),
    ("robots_txt", "robots_txt", lambda c, _: _robots(c)),
    ("analytics", "analytics", lambda c, _: _analytics(c)),
    ("ssl", "https", lambda c, _: _ssl(c)),
    ("mobile", "mobile_viewport", lambda c, _: _mobile(c)),
    ("platform", "platform", lambda c, _: _platform(c)),
    ("cookie_banner", "cookie_banner", lambda c, _: _cookie(c)),
    ("lang", "lang", lambda c, _: _lang(c)),
    ("hreflang", "hreflang", lambda c, _: _hreflang(c)),
    ("broken_links", "broken_links", lambda c, _: _broken(c)),
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
    conf = score.get("confidence")
    if conf in ("low", "none"):
        rating = (f"{score['label']} ({score['value']}/100, LOW CONFIDENCE — only "
                  f"{_plural(score.get('pages_scored') or 0, 'page')} analyzed)")
    elif conf:
        rating = f"{score['label']} ({score['value']}/100, {conf} confidence, {score['pages_scored']} pages)"
    else:
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


def _coverage(report: dict) -> str:
    crawl, cs = report["crawl"], report["content_stats"]
    sm = crawl.get("sitemap", {})
    total, unique = sm.get("url_count"), sm.get("unique_urls")
    text = f"{cs['pages_analyzed']} pages analyzed"
    if unique and unique != total:
        text += f" of {unique} unique sitemap URLs ({total} entries)"
    elif total:
        text += f" of {total} URLs in the sitemap"
    shells = cs.get("empty_template_pages") or 0
    if shells:
        text += f"; {shells} of them are empty template pages, so {cs['pages_analyzed'] - shells} have real content"
    text += f"; {crawl['pages_attempted']} fetched, {crawl['pages_failed']} failed"
    if crawl.get("pages_blocked"):
        text += f", {crawl['pages_blocked']} blocked"
    return text


MAX_QUICK_WINS = 12
MIN_LLM_WINS = 3


def _wins(report: dict) -> list[str]:
    """Plain action strings, most important first. Keeps a few LLM (content/strategy) wins in the list."""
    wins = list(enumerate(report["quick_wins"]))
    llm = [w for w in wins if w[1]["source"] == "llm"]
    rule = [w for w in wins if w[1]["source"] != "llm"]
    llm_in_top = sum(q["source"] == "llm" for _, q in wins[:MAX_QUICK_WINS])
    llm_quota = min(len(llm), max(MIN_LLM_WINS, llm_in_top))
    chosen = sorted(rule[:MAX_QUICK_WINS - llm_quota] + llm[:llm_quota])
    return [q["action"] for _, q in chosen]


def _llm_missing(llm: dict) -> str:
    return f"Not analyzed (LLM {llm['status']}" + (f": {llm['reason']})" if llm.get("reason") else ")")


def summarize(report: dict) -> dict[str, Any]:
    """Full report dict (SiteReport.model_dump(mode='json')) -> compact summary dict."""
    tech, llm = report["technical_seo"], report["llm_analysis"]
    home_h2 = _home_h2(report)
    technical = {}
    for out_key, check_key, fmt in FORMATTERS:
        c = tech.get(check_key)
        if c is None:
            continue
        technical[out_key] = _not_verified(c) or fmt(c, home_h2)

    llm_ok = llm["status"] == "ok"
    cs = report["content_stats"]
    loc = report.get("locale") or {}
    shells = cs.get("empty_template_pages") or 0
    pages = (f"{cs['pages_analyzed'] - shells} pages with real content, avg {cs['avg_word_count']} words of "
             "page-specific text (menus/footers repeated across pages excluded)"
             + (f", {cs['thin_pages']} thin (<300 words)" if cs.get("thin_pages") else "")
             + (f"; {shells} empty template pages excluded" if shells else ""))
    severity = {"high": 0, "medium": 1, "low": 2}
    findings = [f["message"] for f in sorted(report.get("findings") or [], key=lambda f: severity[f["severity"]])]
    return {
        "site": report["site"],
        "company": report.get("company"),
        "analysis_type": "SEO Analysis",
        "sector": report.get("sector"),
        "analyzed_at": report["analyzed_at"],
        "country": loc.get("country"),
        "data_coverage": _coverage(report),
        "overall_rating": _rating(report),
        "score_caveats": report["score"].get("caveats") or [],
        "technical_seo": technical,
        "site_specific_findings": findings,
        "content_seo": {
            "keyword_strategy": llm.get("keyword_strategy") if llm_ok else _llm_missing(llm),
            "captured_keywords": llm.get("captured_keywords") or [],
            "missed_keywords": [k["keyword"] for k in llm.get("missed_keywords") or []],
            "pages": pages,
            "blog": _blog(report),
        },
        "competitive_seo_position": llm.get("competitive_position") if llm_ok else _llm_missing(llm),
        "quick_wins": _wins(report),
    }
