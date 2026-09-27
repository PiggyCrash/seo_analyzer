"""CLI.

    python main.py www.someweb.com
    python main.py site1.com site2.com --force
    python main.py --input urls.txt
    python main.py www.someweb.com --out result.json
    python main.py --serve                  # start the FastAPI server
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

from seo.config import load_settings
from seo.pipeline import AnalysisOutcome, SEOPipeline
from seo.urlutils import normalize_input, site_key


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Crawl a site, run SEO checks + LLM analysis, export JSON.")
    p.add_argument("urls", nargs="*", help="one or more sites, e.g. www.someweb.com")
    p.add_argument("-i", "--input", help="text file with one URL per line (# for comments)")
    p.add_argument("-o", "--out", help="also write the report to this path (single site only)")
    p.add_argument("--force", action="store_true", help="ignore a fresh cached report")
    p.add_argument("--reanalyze", action="store_true", help="re-run extraction + LLM on the last cached crawl")
    p.add_argument("--no-llm", action="store_true", help="deterministic checks only")
    p.add_argument("--max-pages", type=int, help="override crawl.max_pages")
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--serve", action="store_true", help="start the FastAPI server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    return p.parse_args(argv)


def collect_urls(args) -> list[str]:
    raw = list(args.urls)
    if args.input:
        for line in Path(args.input).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.append(line)
    seen, out = set(), []
    for u in raw:
        try:
            key = site_key(normalize_input(u))
        except ValueError as exc:
            print(f"  skipping {u!r}: {exc}")
            continue
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


KEY_PROBLEMS = {
    "quota_exhausted": ("LLM API QUOTA EXHAUSTED",
                        "Your plan/credits for this key are used up. Top up or wait for the quota to reset."),
    "auth_failed": ("LLM API KEY REJECTED",
                    "Check the key in .env and that llm.base_url matches your account's region."),
}


def print_llm_notice(results: list[tuple[str, AnalysisOutcome]], settings) -> None:
    """Loud end-of-run notice when sites went without LLM analysis because of the key/plan."""
    hit: dict[str, list[str]] = {}
    for url, out in results:
        llm = out.report["llm_analysis"]
        if llm.get("error_type") in KEY_PROBLEMS:
            hit.setdefault(llm["error_type"], []).append(url)
    for kind, sites in hit.items():
        title, advice = KEY_PROBLEMS[kind]
        reason = next(o.report["llm_analysis"]["reason"] for _, o in results
                      if o.report["llm_analysis"].get("error_type") == kind)
        line = "!" * 72
        print(f"\n{line}\n  {title}  ({settings.llm.api_key_env}, model {settings.llm.model})")
        print(f"  {reason}\n  {advice}")
        print(f"  {len(sites)} site(s) saved WITHOUT LLM analysis (crawl + score are fine): {', '.join(sites[:10])}"
              + (" ..." if len(sites) > 10 else ""))
        print(f"  Afterwards, add the LLM part without re-crawling:  python main.py {' '.join(sites[:3])}"
              + (" ..." if len(sites) > 3 else "") + " --reanalyze")
        print(line)


def print_outcome(i: int, n: int, url: str, out: AnalysisOutcome) -> None:
    r = out.report
    c, s, llm = r["crawl"], r["score"], r["llm_analysis"]
    high = sum(q["priority"] == "high" for q in r["quick_wins"])
    score = f"{s['value']}/100" if s["value"] is not None else "n/a"
    print(f"\n[{i}/{n}] {r['site']}" + ("  (cached report, use --force to re-run)" if out.from_cache else ""))
    print(f"  crawl : {c['status']}  ({c['pages_ok']} ok / {c['pages_blocked']} blocked / "
          f"{c['pages_failed']} failed, {c['duration_seconds']}s)" + (f"  reason: {c['reason']}" if c.get("reason") else ""))
    print(f"  score : {score} ({s['label']}, coverage {int(s['coverage'] * 100)}%)")
    tag = f" [{llm['error_type'].upper().replace('_', ' ')}]" if llm.get("error_type") else ""
    print(f"  llm   : {llm['status']}{tag}" + (f" ({llm['reason']})" if llm.get("reason") else f" ({llm.get('model')})"))
    print(f"  wins  : {len(r['quick_wins'])} quick wins ({high} high priority)")
    print(f"  saved : {out.path}")


async def run(args) -> int:
    settings = load_settings(args.config)
    if args.no_llm:
        settings.llm.enabled = False
    urls = collect_urls(args)
    if not urls:
        print("No valid URLs given.")
        return 2
    if args.out and len(urls) > 1:
        print("--out only works with a single site (all reports are saved in output/ anyway).")
        return 2

    pipeline = SEOPipeline(settings)
    sem = asyncio.Semaphore(max(1, settings.run.site_concurrency))
    n = len(urls)

    async def one(i: int, url: str):
        async with sem:
            try:
                out = await pipeline.analyze(url, force=args.force, reanalyze=args.reanalyze,
                                             max_pages=args.max_pages)
            except Exception as exc:
                print(f"\n[{i}/{n}] {url}\n  ERROR: {type(exc).__name__}: {exc}")
                logging.getLogger("main").debug("traceback", exc_info=True)
                return None
            print_outcome(i, n, url, out)
            return out

    results = await asyncio.gather(*(one(i + 1, u) for i, u in enumerate(urls)))
    done = [r for r in results if r]
    print_llm_notice([(u, r) for u, r in zip(urls, results) if r and not r.from_cache], settings)
    if args.out and done:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(done[0].report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nExported: {Path(args.out).resolve()}")
    print(f"\nDone: {len(done)}/{n} succeeded.")
    return 0 if len(done) == n else 1


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.serve:
        import uvicorn
        uvicorn.run("api:app", host=args.host, port=args.port)
        return
    if not args.urls and not args.input:
        print("usage: python main.py www.someweb.com   (or --input urls.txt, or --serve)")
        sys.exit(2)
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
