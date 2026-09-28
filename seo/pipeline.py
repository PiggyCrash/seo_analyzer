"""crawl -> extract -> score -> (gated) LLM -> validate -> store. Shared by CLI and API."""
from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from . import __version__
from .config import ROOT_DIR, Settings
from .crawler import Crawler, CrawlResult
from .extractor import extract_page, rollup
from .llm import LLMAnalyzer, LLMUnavailable, build_digest, classify_error
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
        if settings.llm.enabled:
            try:
                self._llm = LLMAnalyzer(settings.llm)
            except LLMUnavailable as exc:
                self._llm_error = str(exc)
                log.warning("LLM disabled: %s", exc)

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
        score = compute_score(tech, content.pages_analyzed)
        rule_wins = rule_quick_wins(tech, content)
        site = crawl.site_key
        llm, llm_wins = await self._run_llm(site, summary, tech, content, locale, records, rule_wins, use_llm)

        return SiteReport(
            tool_version=__version__, site=site, company=llm.company, sector=llm.sector,
            input_url=crawl.input_url, analyzed_at=datetime.now(timezone.utc),
            analysis_seconds=round(time.monotonic() - t0, 2), crawl=summary, score=score,
            technical_seo=tech, content_stats=content, locale=locale, llm_analysis=llm,
            quick_wins=sort_wins(rule_wins + llm_wins),
            pages=records if self.settings.storage.include_pages else [])

    async def _run_llm(self, site, summary, tech, content, locale, records, rule_wins, use_llm):
        cfg = self.settings.llm
        model = cfg.model
        if not use_llm or not cfg.enabled:
            return LLMAnalysis(status="skipped", reason="disabled", model=None), []
        if self._llm is None:
            return LLMAnalysis(status="skipped", reason=self._llm_error, model=model), []

        home_ok = bool(records) and records[0].fetch.outcome == "ok"
        needed = min(cfg.min_ok_pages, max(1, summary.pages_attempted))
        if not home_ok or summary.pages_ok < needed:
            reason = f"insufficient_data: homepage_ok={home_ok}, pages_ok={summary.pages_ok}, needed={needed}"
            return LLMAnalysis(status="skipped", reason=reason, model=model), []

        digest = build_digest(site, summary, tech, content, locale, records, rule_wins, cfg.max_digest_chars)
        try:
            out = await self._llm.analyze(digest, frozenset(q.related_check for q in rule_wins if q.related_check))
        except LLMUnavailable as exc:
            return LLMAnalysis(status="failed", error_type=exc.error_type, reason=str(exc)[:400], model=model), []
        except Exception as exc:
            log.error("LLM failed for %s: %s", site, exc)
            kind = "invalid_output" if isinstance(exc, (ValueError, ValidationError)) else classify_error(exc)
            return LLMAnalysis(status="failed", error_type=kind,
                               reason=f"{type(exc).__name__}: {str(exc)[:300]}", model=model), []
        data = out.model_dump(exclude={"quick_wins"})
        return LLMAnalysis(status="ok", model=model, **data), out.quick_wins
