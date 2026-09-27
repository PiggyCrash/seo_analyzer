"""FastAPI server.  Run:  python main.py --serve   (docs at http://127.0.0.1:8000/docs)"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from seo import __version__
from seo.config import load_settings
from seo.pipeline import SEOPipeline
from seo.summary import summarize
from seo.urlutils import normalize_input, site_key

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["pipeline"] = SEOPipeline(load_settings())
    state["jobs"] = {}
    state["tasks"] = set()
    yield


app = FastAPI(title="SEO Analyzer", version=__version__, lifespan=lifespan)


class AnalyzeRequest(BaseModel):
    url: str = Field(..., examples=["www.someweb.com"])
    force: bool = False
    use_llm: bool = True
    max_pages: Optional[int] = Field(None, ge=1, le=50)
    full: bool = False


def _pipeline() -> SEOPipeline:
    return state["pipeline"]


@app.get("/health")
async def health():
    p = _pipeline()
    problem = p.llm_problem
    if problem:
        llm = {"status": problem.error_type, "detail": str(problem)}
    elif p._llm:
        llm = {"status": "ready"}
    else:
        llm = {"status": "unavailable", "detail": p._llm_error or "disabled"}
    return {"status": "ok", "version": __version__, "llm": llm}


def _llm_warning(report: dict) -> Optional[str]:
    llm = report["llm_analysis"]
    if llm.get("error_type") == "quota_exhausted":
        return f"LLM API quota exhausted; report has no LLM analysis. {llm.get('reason')}"
    if llm.get("error_type") == "auth_failed":
        return f"LLM API key rejected; report has no LLM analysis. {llm.get('reason')}"
    return None


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    """Runs the full pipeline and waits for it (can take 1-3 minutes)."""
    try:
        out = await _pipeline().analyze(req.url, force=req.force, use_llm=req.use_llm, max_pages=req.max_pages)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"from_cache": out.from_cache, "file": str(out.path), "warning": _llm_warning(out.report),
            "report": out.report if req.full else summarize(out.report)}


@app.post("/jobs", status_code=202)
async def create_job(req: AnalyzeRequest):
    """Starts the pipeline in the background; poll GET /jobs/{id}."""
    try:
        normalize_input(req.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    job_id = uuid.uuid4().hex[:12]
    jobs = state["jobs"]
    jobs[job_id] = {"id": job_id, "url": req.url, "status": "queued",
                    "created_at": datetime.now(timezone.utc).isoformat()}

    async def work():
        jobs[job_id]["status"] = "running"
        try:
            out = await _pipeline().analyze(req.url, force=req.force, use_llm=req.use_llm, max_pages=req.max_pages)
            r = out.report
            jobs[job_id].update(status="done", site=r["site"], score=r["score"]["value"],
                                crawl_status=r["crawl"]["status"], file=str(out.path),
                                llm_status=r["llm_analysis"]["status"],
                                llm_error_type=r["llm_analysis"].get("error_type"),
                                warning=_llm_warning(r),
                                report_url=f"/reports/{r['site']}")
        except Exception as exc:
            jobs[job_id].update(status="error", error=f"{type(exc).__name__}: {exc}")

    task = asyncio.create_task(work())
    state["tasks"].add(task)
    task.add_done_callback(state["tasks"].discard)
    return jobs[job_id]


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = state["jobs"].get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/reports")
async def list_reports():
    """Latest run per site, from output/index.jsonl."""
    return _pipeline().storage.list_latest()


@app.get("/reports/{site}")
async def get_report(site: str, full: bool = False):
    """Compact summary by default; ?full=true for every check, crawl detail and page."""
    try:
        key = site_key(normalize_input(site))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    storage = _pipeline().storage
    data = storage.load_latest(key) if full else storage.load_summary(key)
    if not data:
        raise HTTPException(404, f"no report for {key}")
    return data
