"""crawl -> extract -> score -> (gated) LLM -> validate -> store. Shared by CLI and API."""
from __future__ import annotations

import dataclasses
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from . import __version__, fallback
from .config import ROOT_DIR, Settings
from .crawler import Crawler, CrawlResult
from .extractor import boilerplate_text, build_findings, extract_page, rollup
from .llm import MERGEABLE, LLMAnalyzer, LLMUnavailable, build_digest, classify_error, covered_topics
from .schema import LLMAnalysis, PageRecord, SiteReport
from .scoring import compute_score, rule_quick_wins, sort_wins
from .storage import Storage
from .urlutils import normalize_input, site_key

log = logging.getLogger(__name__)


@dataclass
class AnalysisOutcome:
    report: dict
    path: Path
    from_cache: bool


class SEOPipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = Storage(settings.storage, ROOT_DIR)
        self._llm: Optional[LLMAnalyzer] = None
        self._llm_error: Optional[str] = None
        self._fallback: Optional[LLMAnalyzer] = None
        if settings.llm.enabled:
            try:
                self._llm = LLMAnalyzer(settings.llm)
            except LLMUnavailable as exc:
                self._llm_error = str(exc)
                log.warning("LLM disabled: %s", exc)
            fb = settings.llm.fallback_config()
            if fb:
                try:
                    self._fallback = LLMAnalyzer(fb)
                except LLMUnavailable as exc:
                    log.warning("fallback LLM disabled: %s", exc)

    @property
    def llm_problem(self) -> Optional[LLMUnavailable]:
        """Current quota/auth problem with the LLM API, if any (for CLI notices and /health)."""
        return self._llm.problem if self._llm else None

    async def analyze(self, url: str, *, force: bool = False, reanalyze: bool = False,
                      use_llm: bool = True, max_pages: Optional[int] = None) -> AnalysisOutcome:
        start = normalize_input(url)
        key = site_key(start)

        if not force and not reanalyze:
            fresh = self.storage.load_fresh(key, self.settings.storage.skip_if_fresh_hours)
            if fresh:
                return AnalysisOutcome(fresh, self.storage.site_file(key), True)

        t0 = time.monotonic()
        if reanalyze:
            crawl = self.storage.load_latest_crawl(key)
            if crawl is None:
                raise ValueError(f"no cached crawl for {key}; run once without --reanalyze")
        else:
            cfg = self.settings.crawl
            if max_pages:
                cfg = dataclasses.replace(cfg, max_pages=max_pages)
            crawl = await Crawler(cfg).crawl_site(url)
            self.storage.save_crawl(crawl)

        report = await self._build_report(crawl, use_llm, t0)
        path = self.storage.save_report(report)
        return AnalysisOutcome(report.model_dump(mode="json"), path, False)

    async def _build_report(self, crawl: CrawlResult, use_llm: bool, t0: float) -> SiteReport:
        summary = crawl.summary()
        records = []
        for i, page in enumerate(crawl.pages):
            facts = None
            if page.outcome == "ok" and page.text:
                facts = extract_page(page.url, page.final_url, page.text, page.headers, is_home=(i == 0))
            records.append(PageRecord(fetch=page.to_model(), facts=facts))

        tech, content, locale = rollup(records, summary)
        findings = build_findings(records, summary, tech)
        score = compute_score(tech, content.pages_analyzed, findings)
        rule_wins = rule_quick_wins(tech, content, findings)
        site = crawl.site_key
        llm, llm_wins = await self._run_llm(site, summary, tech, content, locale, records, rule_wins, findings,
                                            use_llm)
        # An LLM win on a mergeable topic (e.g. schema with the real legal name/ticker) replaces the generic rule win.
        replaced = {q.related_check for q in llm_wins
                    if q.related_check in MERGEABLE and re.search(r"organi[sz]ation", q.action, re.I)}
        rule_wins = [q for q in rule_wins if q.related_check not in replaced]
        company = llm.company or _brand(records) or _domain_name(site)
        before = llm.model_dump()
        llm = fallback.fill(llm, records, tech, content, locale, findings, score, summary, site, company)
        filled = [k for k, v in llm.model_dump().items() if v != before.get(k)]
        if filled:
            llm = llm.model_copy(update={"rule_filled": filled})

        return SiteReport(
            tool_version=__version__, site=site, company=company, sector=llm.sector,
            input_url=crawl.input_url, analyzed_at=datetime.now(timezone.utc),
            analysis_seconds=round(time.monotonic() - t0, 2), crawl=summary, score=score,
            technical_seo=tech, content_stats=content, locale=locale, llm_analysis=llm, findings=findings,
            quick_wins=sort_wins(rule_wins + llm_wins),
            pages=records if self.settings.storage.include_pages else [])

    async def _run_llm(self, site, summary, tech, content, locale, records, rule_wins, findings, use_llm):
        cfg = self.settings.llm
        model = cfg.model
        if not use_llm or not cfg.enabled:
            return LLMAnalysis(status="skipped", reason="disabled", model=None), []
        providers = [a for a in (self._llm, self._fallback) if a is not None]
        if not providers:
            return LLMAnalysis(status="skipped", reason=self._llm_error, model=model), []

        home_ok = bool(records) and records[0].fetch.outcome == "ok"
        needed = min(cfg.min_ok_pages, max(1, summary.pages_attempted))
        if not home_ok or summary.pages_ok < needed:
            reason = f"insufficient_data: homepage_ok={home_ok}, pages_ok={summary.pages_ok}, needed={needed}"
            return LLMAnalysis(status="skipped", reason=reason, model=model), []

        nav = frozenset(t.casefold() for t in boilerplate_text([r.facts for r in records if r.facts]))
        failure: Optional[LLMAnalysis] = None
        for analyzer in providers:
            pcfg = analyzer.cfg
            digest = build_digest(site, summary, tech, content, locale, records, rule_wins, pcfg.max_digest_chars,
                                  findings)
            try:
                out = await analyzer.analyze(digest, covered_topics(rule_wins), nav)
            except LLMUnavailable as exc:
                failure = LLMAnalysis(status="failed", error_type=exc.error_type, reason=str(exc)[:400],
                                      model=pcfg.model)
            except Exception as exc:
                log.error("LLM %s failed for %s: %s", pcfg.model, site, exc)
                kind = "invalid_output" if isinstance(exc, (ValueError, ValidationError)) else classify_error(exc)
                failure = LLMAnalysis(status="failed", error_type=kind, model=pcfg.model,
                                      reason=f"{type(exc).__name__}: {str(exc)[:300]}")
            else:
                data = out.model_dump(exclude={"quick_wins"})
                note = f"fallback after {failure.model}: {failure.reason[:120]}" if failure else None
                return LLMAnalysis(status="ok", model=pcfg.model, reason=note, **data), out.quick_wins
            if len(providers) > 1 and analyzer is providers[0]:
                log.warning("LLM %s failed for %s, trying fallback %s", pcfg.model, site, providers[1].cfg.model)
        return failure, []


def _domain_name(site: str) -> str:
    """Last-resort company name from the domain: 'dexagroup.com' -> 'Dexagroup'."""
    return site.split(".")[0].split("_")[0].replace("-", " ").title()


def _brand(records: list[PageRecord]) -> Optional[str]:
    """Company name without the LLM: Organization JSON-LD name, then og:site_name (when it is not a URL)."""
    facts = [r.facts for r in records if r.facts]
    name = next((f.org_name for f in facts if f.org_name), None)
    if name:
        return name
    site_name = facts[0].og.get("og:site_name") if facts else None
    if site_name and not site_name.lower().startswith(("http", "www.")):
        return site_name
    # A homepage title segment that spells the domain ("Zero Penny - ..." on zeropenny.co).
    home_url = (records[0].fetch.final_url or records[0].fetch.url) if records else ""
    domain = re.sub(r"[^a-z0-9]", "", home_url.split("//")[-1].split("/")[0].lower().replace("www.", "")
                    .rsplit(".", 1)[0])
    for seg in re.split(r"\s[|\-–—·»]\s", facts[0].title or "") if facts else []:
        if domain and re.sub(r"[^a-z0-9]", "", seg.lower()) == domain.split(".")[0]:
            return seg.strip()
    # The brand the template puts before or after every title ("About Us | McDermott", "Kimia Farma | Profil").
    tails = Counter(part.strip() for f in facts if f.title and re.search(r"\s[|\-–—·»]\s", f.title)
                    for part in {re.split(r"\s[|\-–—·»]\s", f.title)[0], re.split(r"\s[|\-–—·»]\s", f.title)[-1]})
    if tails:
        tail, count = tails.most_common(1)[0]
        if count >= max(3, len(facts) // 2) and 1 < len(tail) <= 40:
            return tail
    return None
