# SEO Analyzer

Crawls a website (homepage + ~15 key pages) and runs deterministic SEO checks. Then an LLM (Groq's free tier by default; any OpenAI-compatible API such as MiniMax or Cerebras works) interprets the results, and everything is exported as validated JSON. It scales from 1 site to thousands.

```
URL -> crawler -> extractor (facts) -> score + rule quick wins -> LLM (interpretation) -> JSON
```

Code measures facts. The LLM only interprets them, so it can't invent a missing tag or change the score.

## Setup (Windows)

```powershell
cd C:\Users\gashandyd\Downloads\Project\seo_analysis
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then put your GROQ_API_KEY in .env (free: console.groq.com/keys)
```

Open `config.yaml` and set `crawl.user_agent` to include your real contact email. Also check that `llm.model` matches a model your key can use (`GET https://api.groq.com/openai/v1/models`). Groq's free tier allows 8,000 tokens/minute, so the defaults run about one LLM call per minute. To use MiniMax or Cerebras instead, switch the commented `llm.*` lines in `config.yaml`.

## Usage

```powershell
python main.py www.someweb.com                  # analyze one site
python main.py www.someweb.com --out result.json  # also export the summary to a specific file (add --full for everything)
python main.py site1.com site2.com site3.com    # several sites (3 in parallel)
python main.py --input urls.txt                 # one URL per line, # = comment
python main.py www.someweb.com --force          # ignore a report younger than 24h
python main.py www.someweb.com --reanalyze      # re-run checks + LLM on the cached crawl (no re-scraping)
python main.py www.someweb.com --no-llm         # deterministic checks only, no API cost
python main.py www.someweb.com --max-pages 20 -v
```

### FastAPI server

```powershell
python main.py --serve          # http://127.0.0.1:8000/docs
```

| Endpoint | Purpose |
|---|---|
| `POST /analyze` `{"url": "www.someweb.com"}` | Run and wait (1–3 min) |
| `POST /jobs` → `GET /jobs/{id}` | Run in the background and poll |
| `GET /reports` | Latest result per site |
| `GET /reports/{site}` | Summary JSON for one site (`?full=true` for the full report) |
| `GET /health` | Status, and whether the LLM key is loaded |

## Output

```
output/sites/<site>.json               latest report per site: short summary, one sentence per check
output/history/<site>/<timestamp>.json every run, full detail (all checks, crawl, pages), never overwritten
output/index.jsonl                     one summary line per run: load this for dashboards / bulk queries
cache/<site>/<timestamp>/              raw HTML + crawl metadata (used by --reanalyze)
```

Each check has a fixed `status`: `present | partial | missing | not_checked | error`. `not_checked` always comes with a `reason`. **A blocked page is never reported as "missing".** Each report records `schema_version`, `tool_version` and the model used, so runs stay comparable.

The score is computed from weighted checks (`seo/scoring.py`) and is reproducible. If less than 50% of the weighted checks could be verified, the score is `null` with the label "Insufficient data".

Quick wins have `source: "rule"` (derived from the facts) or `source: "llm"` (strategy/content), plus `priority`, `effort` and `category`. LLM keyword suggestions are tagged `llm_suggested` because they are hypotheses, not search-volume data.

## When the API key runs out

LLM errors are classified, and the crawl and score are always saved:

| `llm_analysis.error_type` | Meaning | Behaviour |
|---|---|---|
| `quota_exhausted` | Plan/credits/daily quota used up (e.g. MiniMax `2056`, HTTP 402, "tokens per day") | No retries. The LLM is skipped for the remaining sites for `quota_cooldown_seconds` |
| `auth_failed` | Key rejected (HTTP 401/403, wrong region endpoint) | Same as above |
| `transient` | Per-minute 429, 5xx, timeout | Retried with backoff (`max_retries`) |
| `invalid_output` | Model returned unusable JSON twice | Report saved without LLM part |

The CLI prints a `!!! LLM API QUOTA EXHAUSTED !!!` notice listing the affected sites. The API adds a `warning` to `/analyze` and `/jobs/{id}`, and `/health` reports `llm.status: "quota_exhausted"`. Once the key works again, run `python main.py <site> --reanalyze` to add the LLM part without re-crawling.

## Crawl behavior and blockers

The crawler respects `robots.txt` and `Crawl-delay`, sends at most 2 concurrent requests per site with a delay between them, identifies itself with an honest User-Agent, and never impersonates Googlebot.

It **detects and records blockers without bypassing them**:

| Situation | Recorded as | What happens |
|---|---|---|
| Cloudflare / DataDome / Incapsula challenge | `anti_bot` | Crawl stops for that site |
| CAPTCHA page | `captcha` | Crawl stops |
| HTTP 429 | `rate_limited` | Honors Retry-After; stops after repeated 429s |
| 401 / redirect to login | `auth_required` | Page skipped |
| 451 / "not available in your country" | `geo` | Page skipped |
| Age gate hiding the content | `age_gate` (blocked) | Page skipped; an overlay with readable content is only flagged |
| JS-only app, empty HTML | flag `needs_js_render` | Recorded for a future Playwright fallback |
| 200 page that says "not found" | `soft_404` | Excluded from checks |
| DNS / SSL / timeout | `dns_error` / `ssl_error` / `timeout` | Tries http and www/non-www variants first |

Crawl `status`: `ok | partial | blocked | failed`. The LLM is skipped when the homepage is unusable or fewer than `min_ok_pages` pages came back, so no tokens are spent analyzing a challenge page.

Page selection ranks about / services / pricing / contact / blog / case-study pages first. It caps blog posts at about 40% of slots and skips login, cart, tag, pagination, file and tracking-parameter URLs.

## Offline test site

```powershell
python tests\fixture_site.py              # terminal 1: fake site on 127.0.0.1:8765
python main.py 127.0.0.1:8765 --no-llm --force   # terminal 2
```

The fixture includes an anti-bot page, a 429, an age gate, a login redirect, a soft 404, a noindex page and tracking pixels. Set `FIXTURE_MODE=blocked` to simulate a fully blocked site.

## Project layout

```
main.py            CLI (+ --serve)
api.py             FastAPI app
config.yaml        all tunables
seo/crawler.py     fetching, robots, sitemap, page selection, retries, budgets
seo/blockers.py    blocker + flag detection
seo/extractor.py   per-page facts and site rollup into checks
seo/scoring.py     weighted score + rule-based quick wins
seo/llm.py         Cerebras client, prompt, digest, JSON repair
seo/schema.py      Pydantic models = the JSON contract
seo/storage.py     reports, history, index, crawl cache
seo/pipeline.py    ties it together (used by CLI and API)
```

## Known limits

- Plain HTTP fetching, no JavaScript execution. Server-rendered sites (Webflow, WordPress, Shopify) work fully. Pure SPAs get flagged instead.
- Cookie-banner detection reads static HTML, so banners injected only via GTM can be missed. The note in the report says so.
- No search-volume or ranking data. To add it, connect Google Search Console or a keyword API later.
