"""Rule-based stand-ins for the LLM fields, so a report never ships with empty sector / keyword / position
fields. Used when every LLM provider failed or was skipped, and to top up fields an LLM left empty.
Everything here is derived from measured facts; keyword ideas are marked as rule-suggested hypotheses."""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from .extractor import NAV_HEADING, boilerplate_text
from .schema import (ContentStats, CrawlSummary, Finding, KeywordSuggestion, LLMAnalysis, Locale, PageFacts,
                     PageRecord, Score, TechnicalSEO)

COUNTRY_NAMES = {"ID": "Indonesia", "SG": "Singapore", "MY": "Malaysia", "TH": "Thailand", "PH": "Philippines",
                 "VN": "Vietnam", "AU": "Australia", "NZ": "New Zealand", "GB": "UK", "IN": "India", "US": "USA",
                 "HK": "Hong Kong", "JP": "Japan", "KR": "Korea", "BR": "Brazil", "CA": "Canada"}
GENERIC_SEGMENTS = re.compile(
    r"^(home|homepage|beranda|about( us)?|tentang( kami)?|contact( us)?|hubungi( kami)?|blog|news|berita|careers?"
    r"|karir|privacy( policy)?|terms.*|faq|journal|profil|profile|login|search|404.*|page not found|services?|layanan"
    r"|products?|produk|projects?|proyek|team|our team|mission.*|visi.*)$", re.I)
MODIFIERS = {
    "en": ["{core} company {country}", "{core} cost", "best {core} {country}", "{core} case study",
           "how to choose {article} {core} provider"],
    "id": ["harga {core}", "jasa {core} {country}", "perusahaan {core} terbaik", "biaya {core}", "cara memilih {core}"],
}
MODIFIER_WORDS = {"en": ("company", "cost", "best", "case study", "choose"),
                  "id": ("harga", "jasa", "terbaik", "biaya", "memilih")}
TRAILING_FILLER = re.compile(r"\b(at|for|of|and|to|with|in|on|by|dan|di|untuk)$", re.I)
COUNTRY_WORDS = re.compile(r"\b(in |di )?(singapore|indonesia|malaysia|thailand|philippines|vietnam|australia|india|"
                           r"usa|uk|global)\b", re.I)
LEADING_FILLER = re.compile(r"^(at|for|a|an|in|on|to|with|and|by|of|di|ke|dan|untuk|your|our)\b", re.I)
# Press-release verbs: headlines are news, not the keywords a site targets.
HEADLINE = re.compile(r"\b(raih|wins?|won|launch(es|ed)?|luncurkan|announces?|signs?|completes?|awarded|named|"
                      r"meresmikan|tuntaskan|dorong|perkuat|konsisten|celebrates?|partners with)\b", re.I)
LEADING_NOISE = re.compile(r"^(about|tentang|profil|welcome|selamat datang|our|the|home|why|how|what|meet|get in touch"
                           r"|contact|hubungi|we|kami|let'?s|join|discover|explore|learn)\b", re.I)
IS_A = re.compile(r"(?P<subj>[\w&.'’\- ]{2,40}?)\s+(?:is|are|adalah|merupakan)\s+"
                  r"(?:(?:an|a|the|one of the|salah satu)\s+)?(?P<what>[^.;:!?]{15,140})", re.I)
CORPORATE = re.compile(r"\b(investor|relations|accessibility|statement|sponsorships?|results|careers?|policy|terms|"
                       r"cookies?|sitemap|newsroom|press|disclaimer|karir|kebijakan|syarat)\b", re.I)
SELF_WORDS = {"we", "kami", "company", "perusahaan", "firm", "agency", "group"}
NOT_A_SECTOR = re.compile(r"^(not|committed|dedicated to ensuring|proud|here|available|required|used|the best way)\b", re.I)
NEWS_PAGE_TYPES = {"blog_post", "blog_index"}
RULE_NOTE = " (rule-based: no LLM output for this run)"


def _trim(text: str, limit: int = 110) -> str:
    """Cut at a word boundary instead of mid-word."""
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;-")


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" -|–—:·»,")
    return text if not text.isupper() else text.capitalize()


def _brand_tokens(company: Optional[str], site: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", (company or "").lower())) | {site.split(".")[0].lower()}
    return {w for w in words if len(w) > 1}


def _segments(f: PageFacts) -> list[str]:
    parts = re.split(r"\s[|\-–—:·»]\s|:\s", f.title or "")
    return [p for p in parts if p] + [p for h in f.h1[:1] for p in re.split(r":\s", h)]


def _is_brand(text: str, brand: set[str]) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower())
             if w not in {"the", "pt", "tbk", "ltd", "inc", "persero", "group", "co", "com"}]
    return bool(words) and (set(words) <= brand or "".join(words) in brand)


def _strip_brand(text: str, brand: set[str]) -> str:
    """'kimia farma apotek' -> 'apotek' (removes the brand however it is spaced)."""
    for b in sorted(brand, key=len, reverse=True):
        text = re.sub(r"\b" + r"\s*".join(re.escape(ch) for ch in b) + r"\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -|:,")


def sector(facts: list[PageFacts], home: PageFacts, company: Optional[str], site: str) -> str:
    """'is a leading provider of ...' from a meta description or copy, else the homepage tagline."""
    brand = _brand_tokens(company, site)
    ordered = [home] + [f for f in facts if f.page_type == "about"] + facts
    for source in ("meta", "excerpt"):
        for f in ordered:
            texts = (f.meta_description, f.og.get("og:description")) if source == "meta" else (f.text_excerpt[:1500],)
            for text in texts:
                for m in IS_A.finditer(text or ""):
                    subj = set(re.findall(r"[a-z0-9]+", m.group("subj").lower())[-3:])
                    what = m.group("what")
                    # The sentence must describe the organisation itself ("McDermott is ...", "We are ...").
                    if not (subj & (brand | SELF_WORDS) or _is_brand(m.group("subj"), brand)):
                        continue
                    if NOT_A_SECTOR.match(what) or _is_brand(what, brand):
                        continue
                    return _trim(_clean(what))
    for seg in _segments(home) + home.h2[:3]:
        if len(seg.split()) >= 2 and not _is_brand(seg, brand) and not GENERIC_SEGMENTS.match(seg.strip()) \
                and not NAV_HEADING.match(seg):
            return _clean(seg)[:110]
    return _clean(home.meta_description or home.title or site)[:110]


def _strip_country(text: str) -> str:
    return re.sub(r"\s+", " ", COUNTRY_WORDS.sub("", text)).strip(" &,-")


def captured_keywords(facts: list[PageFacts], home: PageFacts, company: Optional[str], site: str,
                      nav: set[str]) -> list[str]:
    """Phrases the site already targets in titles, H1s and homepage H2s (minus brand, menu labels)."""
    brand = _brand_tokens(company, site)
    candidates = Counter()
    for f in [home] + facts:
        if f.empty_template or f.page_type in NEWS_PAGE_TYPES:
            continue
        for seg in _segments(f):
            seg = _clean(_strip_brand(_clean(seg), brand))
            words = seg.split()
            if not 2 <= len(words) <= 7 or len(seg) > 60 or seg.endswith(("?", ".", "!")):
                continue
            if _is_brand(seg, brand) or GENERIC_SEGMENTS.match(seg) or NAV_HEADING.match(seg) \
                    or seg.casefold() in nav or LEADING_NOISE.match(seg) or LEADING_FILLER.match(seg) \
                    or re.search(r"\b(19|20)\d{2}\b", seg) or HEADLINE.search(seg) or TRAILING_FILLER.search(seg) \
                    or CORPORATE.search(seg) or len(_strip_country(seg).split()) < 2:
                continue
            key = seg.lower()
            candidates[key] += 2 if f is home else 1
    return [k for k, _ in candidates.most_common(8)]


def missed_keywords(captured: list[str], text_lower: str, locale: Locale) -> list[str]:
    """Commercial-intent modifier gaps around the site's own core service phrases."""
    lang = "id" if (locale.content_language or "en") == "id" else "en"
    country = COUNTRY_NAMES.get(locale.country or "", "")
    cores = []
    latin = re.compile(r"^[a-z0-9 &/\-.,'’]+$")
    marketing = re.compile(r"^(trusted|licensed|leading|best|top|premier|reliable|professional|mom-licensed|terpercaya)\s+", re.I)
    for c in captured:
        core = _strip_country(c)
        core = marketing.sub("", core).replace(" & ", " and ")
        core = re.sub(r"\s*[,:].*$", "", core).strip()
        if 2 <= len(core.split()) <= 5 and core not in cores and latin.match(core) and not CORPORATE.search(core):
            cores.append(core)
    out = []
    for core in cores[:3]:
        for pattern, word in zip(MODIFIERS[lang], MODIFIER_WORDS[lang]):
            if word in text_lower and word not in ("best", "terbaik"):
                continue
            article = "an" if core[:1] in "aeiou" else "a"
            phrase = re.sub(r"\s+", " ", pattern.format(core=core, country=country.lower(), article=article)).strip()
            if phrase not in out:
                out.append(phrase)
    return out[:8]


def keyword_strategy(captured: list[str], tech: TechnicalSEO, n: int) -> str:
    t, d, h = tech.meta_title.details, tech.meta_description.details, tech.h1.details
    target = ", ".join(f"'{c}'" for c in captured[:3]) or "the brand name only"
    issues = []
    if t.get("duplicates"):
        issues.append(f"{t['duplicates']} duplicated titles")
    if d.get("duplicates"):
        issues.append(f"{d['duplicates']} duplicated meta descriptions")
    if d.get("missing"):
        issues.append(f"{d['missing']} missing meta descriptions")
    if h.get("missing"):
        issues.append(f"{h['missing']} pages without an H1")
    tail = f"; {', '.join(issues)} across the {n} pages sampled weaken page-level targeting" if issues else ""
    return (f"Titles and H1s target {target}{tail}. Keyword ideas are modifier gaps around these phrases, not "
            "search-volume data" + RULE_NOTE)


def competitive_position(score: Score, content: ContentStats, tech: TechnicalSEO, findings: list[Finding]) -> str:
    high = [f.message.split(":")[0] for f in findings if f.severity == "high"][:2]
    blog = "an active content section" if content.blog_detected and content.blog_posts_crawled else "no active blog"
    parts = [f"technical hygiene {score.value}/100 ({score.label})" if score.value is not None else
             "technical hygiene not scored",
             f"{content.pages_analyzed - content.empty_template_pages} content pages averaging "
             f"{content.avg_word_count} words", f"schema {tech.schema_markup.status}", blog]
    risk = f". Most urgent: {'; '.join(high)}" if high else ""
    return ("Measured position: " + ", ".join(parts) + risk + ". Named competitors and winnable queries need the "
            "LLM step (run again with --reanalyze)" + RULE_NOTE)


def summary(score: Score, findings: list[Finding], content: ContentStats, n_sitemap: Optional[int]) -> str:
    top = [f.message for f in sorted(findings, key=lambda f: {"high": 0, "medium": 1, "low": 2}[f.severity])][:2]
    sample = f"{content.pages_analyzed} pages sampled" + (f" of {n_sitemap} in the sitemap" if n_sitemap else "")
    return f"Based on {sample}." + (" Key issues: " + " | ".join(top) if top else " No critical site-specific defect found.")


def fill(llm: LLMAnalysis, records: list[PageRecord], tech: TechnicalSEO, content: ContentStats, locale: Locale,
         findings: list[Finding], score: Score, crawl: CrawlSummary, site: str, company: Optional[str]) -> LLMAnalysis:
    """Return llm with every empty narrative field filled from the facts (LLM values are kept as they are)."""
    facts = [r.facts for r in records if r.facts]
    home = records[0].facts if records and records[0].facts else None
    if not facts or home is None:
        reason = crawl.reason or crawl.status
        blocked = f"Not assessable: the crawler could not read the site ({reason})"
        brand = (company or site.split(".")[0]).lower()
        return llm.model_copy(update={
            "captured_keywords": llm.captured_keywords or [f"{brand} (brand name; page content unreadable)"],
            "missed_keywords": llm.missed_keywords or [
                KeywordSuggestion(keyword=f"{brand} {s}", intent="navigational",
                                  rationale="brand navigational query; not verified because the site blocked the crawler")
                for s in ("products", "services", "contact", "careers")],
            "sector": llm.sector or f"Unknown ({reason})",
            "keyword_strategy": llm.keyword_strategy or blocked,
            "competitive_position": llm.competitive_position or blocked,
            "summary": llm.summary or blocked})
    nav = {t.casefold() for t in boilerplate_text(facts)}
    captured = llm.captured_keywords or captured_keywords(facts, home, company, site, nav)
    text_lower = " ".join(f.text_excerpt for f in facts).lower()
    missed = llm.missed_keywords or [KeywordSuggestion(keyword=k, intent="commercial",
                                                       rationale="rule-suggested modifier gap")
                                     for k in missed_keywords(captured, text_lower, locale)]
    return llm.model_copy(update={
        "sector": llm.sector or sector(facts, home, company, site),
        "captured_keywords": captured,
        "missed_keywords": missed,
        "keyword_strategy": llm.keyword_strategy or keyword_strategy(captured, tech, len(facts)),
        "competitive_position": llm.competitive_position or competitive_position(score, content, tech, findings),
        "summary": llm.summary or summary(score, findings, content, crawl.sitemap.url_count),
        "content_assessment": llm.content_assessment,
    })


def headline(text: str, limit: int = 140) -> str:
    """First clause of an action, cut at a clause boundary: short enough to scan."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    for sep in ("; ", " — ", ": ", ", "):
        cut = text[:limit].rfind(sep)
        if cut > limit * 0.5:
            return text[:cut].rstrip(" ,;:—")
    return text[:limit].rsplit(" ", 1)[0] + "…"


def executive_summary(wins, findings: list[Finding]) -> list[str]:
    """Three bullets from the highest-priority quick wins (already ranked), for when no LLM wrote them."""
    return [headline(q.action, 120) for q in list(wins)[:3]]


def entity_rich_content(schema_details: dict, company: Optional[str] = None) -> str:
    """Coworker-style 'entity_rich_content': what the structured data says about the organisation."""
    d = schema_details or {}
    types = list((d.get("types_found") or {}).keys())
    if not types:
        return ("Thin — no structured data, so the organisation, its address, contact points and people exist only "
                "as page text" + (f" (brand '{company}')" if company else ""))
    org = d.get("organization_name")
    parts = [f"Organization '{org}' in JSON-LD" if org else "No Organization entity in JSON-LD"]
    if d.get("organization_missing_fields"):
        parts.append("missing " + ", ".join(d["organization_missing_fields"]))
    extra = [t for t in ("Person", "Product", "Service", "Review", "AggregateRating", "FAQPage", "Event") if t in types]
    parts.append("other entity types: " + (", ".join(extra) if extra else "none (no Person, Service, Review)"))
    if "Person" not in types:
        parts.append("no named people (founders, authors) marked up")
    return "; ".join(parts)
