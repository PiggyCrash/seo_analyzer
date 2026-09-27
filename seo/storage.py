"""File storage.

output/sites/<site>.json               latest report per site, compact summary (seo/summary.py)
output/history/<site>/<timestamp>.json every run, full detail, never overwritten
output/index.jsonl                     one summary line per run (append-only, easy to load at scale)
cache/<site>/<timestamp>/              raw crawl (crawl.json + pages/*.html) for --reanalyze
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import StorageConfig
from .crawler import CrawlResult
from .schema import SiteReport
from .summary import summarize

_lock = threading.Lock()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Storage:
    def __init__(self, cfg: StorageConfig, root: Path):
        self.cfg = cfg
        self.out = (root / cfg.output_dir).resolve()
        self.sites = self.out / "sites"
        self.history = self.out / "history"
        self.index = self.out / "index.jsonl"
        self.cache = (root / cfg.cache_dir).resolve()

    def site_file(self, key: str) -> Path:
        return self.sites / f"{key}.json"

    def save_report(self, report: SiteReport) -> Path:
        data = report.model_dump(mode="json")
        text = json.dumps(data, ensure_ascii=False, indent=2)
        short = json.dumps(summarize(data), ensure_ascii=False, indent=2)
        path = self.site_file(report.site)
        hist = self.history / report.site / f"{_stamp(report.analyzed_at)}.json"
        line = {
            "site": report.site, "analyzed_at": data["analyzed_at"],
            "crawl_status": report.crawl.status, "score": report.score.value,
            "label": report.score.label, "pages_ok": report.crawl.pages_ok,
            "llm_status": report.llm_analysis.status, "sector": report.sector,
            "quick_wins": len(report.quick_wins),
            "high_priority_wins": sum(q.priority == "high" for q in report.quick_wins),
            "file": str(hist.relative_to(self.out)),
        }
        with _lock:
            _atomic_write(path, short)
            _atomic_write(hist, text)
            self.out.mkdir(parents=True, exist_ok=True)
            with self.index.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        return path

    def load_latest(self, key: str) -> Optional[dict]:
        """Latest full report (from history/)."""
        folder = self.history / key
        runs = sorted(folder.glob("*.json")) if folder.exists() else []
        if not runs:
            return None
        return json.loads(runs[-1].read_text(encoding="utf-8"))

    def load_summary(self, key: str) -> Optional[dict]:
        """Latest compact summary (from sites/)."""
        path = self.site_file(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def load_fresh(self, key: str, hours: float) -> Optional[dict]:
        if hours <= 0:
            return None
        data = self.load_latest(key)
        if not data:
            return None
        try:
            at = datetime.fromisoformat(data["analyzed_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return None
        return data if datetime.now(timezone.utc) - at < timedelta(hours=hours) else None

    def list_latest(self) -> list[dict]:
        """Latest index line per site (cheap even with thousands of sites)."""
        if not self.index.exists():
            return []
        latest: dict[str, dict] = {}
        with self.index.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        row = json.loads(line)
                        latest[row["site"]] = row
                    except (ValueError, KeyError):
                        continue
        return sorted(latest.values(), key=lambda r: r["analyzed_at"], reverse=True)

    def save_crawl(self, crawl: CrawlResult) -> Path:
        folder = self.cache / crawl.site_key / _stamp(datetime.now(timezone.utc))
        pages_dir = folder / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        data = crawl.to_cache()
        for i, (page, entry) in enumerate(zip(crawl.pages, data["pages"])):
            if page.text:
                name = f"{i:02d}.html"
                (pages_dir / name).write_text(page.text, encoding="utf-8")
                entry["html_file"] = name
        _atomic_write(folder / "crawl.json", json.dumps(data, ensure_ascii=False, indent=2))
        return folder

    def load_latest_crawl(self, key: str) -> Optional[CrawlResult]:
        base = self.cache / key
        if not base.exists():
            return None
        runs = sorted((p for p in base.iterdir() if (p / "crawl.json").exists()), reverse=True)
        if not runs:
            return None
        data = json.loads((runs[0] / "crawl.json").read_text(encoding="utf-8"))
        texts = []
        for entry in data.get("pages", []):
            f = entry.get("html_file")
            texts.append((runs[0] / "pages" / f).read_text(encoding="utf-8") if f else "")
        return CrawlResult.from_cache(data, texts)
