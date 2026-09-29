# SEO Analyzer

Crawls a website (homepage + ~15 key pages) and runs deterministic SEO checks. Then an LLM (MiniMax-M3 by default, Groq's free tier as fallback; any OpenAI-compatible API works) interprets the results, and everything is exported as validated JSON. It scales from 1 site to thousands.

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
copy .env.example .env      # then put MINIMAX_API_KEY (and GROQ_API_KEY for the fallback) in .env
```

Open `config.yaml` and set `crawl.user_agent` to include your real contact email. Also check that `llm.model` matches a model your key can use (`GET https://api.minimax.io/v1/models`). `config.yaml` uses MiniMax-M3 with Groq as `llm.fallback`; `config.groq.yaml` does the reverse (`--config config.groq.yaml`). Groq's free tier allows 8,000 tokens/minute, so it runs about one LLM call per minute.

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

### Checks

- Each check has a fixed `status`: `present | partial | missing | not_checked | error`.
- `not_checked` always comes with a `reason`. **A blocked page is never reported as "missing".**
- Each report records `schema_version`, `tool_version` and the model used, so runs stay comparable.
- The crawler also requests a URL that cannot exist (at the root and under the main locale prefix, e.g. `/id/seo-probe-xxxx`):
  - a 200 answer is reported as `soft_404`;
  - crawled pages that match that empty template are marked `empty_template` and left out of content stats.
- The sitemap check reports duplicate, malformed and truncated entries, dead child sitemaps, page-builder template sitemaps and the obsolete 0.84 namespace. For dead URLs under a locale prefix (`/en-GB/`) it retries the same slug under the prefixes the site does serve (`/en/`), so a sitemap-generator bug is not reported as broken links.
- `broken_links` reports 404/410 URLs among the crawled pages plus a spread-out sample of sitemap URLs.
- `internal_linking` reports card grids whose "Read More" links all point to one listing page, sampled pages nothing links to, the share of generic anchors, and internal nofollow links.
- Word counts and LLM excerpts use page-specific text only: text repeated on at least half of the crawled pages (menus, footers) is removed, even on sites that do not use `<nav>`/`<footer>` tags.
- An empty `<h1></h1>` counts as no H1.

### Score

- Computed from weighted checks (`seo/scoring.py`) and reproducible: the same crawl always gives the same score.
- Deductions, so the number agrees with what an expert sees (total at most -25):
  - site-specific findings: -5 per high-severity finding (placeholder text, typo, broken contact links, duplicate pages...), -2 per medium one, at most -20;
  - content gaps: thin pages (-4/-6), under 250 words per page on average (-3), no service/product/case-study pages in the sample (-3), no active blog or news (-2), empty template pages (-3), at most -12.
- `score.technical_value` keeps the pure technical-hygiene number, `score.penalties` lists every deduction, and a caveat explains them in one sentence.
- `null` with the label "Insufficient data" when less than 50% of the weighted checks could be verified.
- `confidence`: `high` ≥10 pages, `medium` ≥5, `low` below that. The summary's `overall_rating` says LOW CONFIDENCE when only a few pages loaded.
- Rankings, backlinks and search volume are not measured.

### Summary fields

- No summary field is left empty:
  - if the main LLM fails, the `llm.fallback` provider in `config.yaml` is tried (MiniMax falls back to Groq);
  - if both fail or the LLM is off, `seo/fallback.py` fills sector, keyword strategy, captured/missed keywords, competitive position, the summary and the executive summary from the measured facts;
  - those texts say "(rule-based ...)", and the full report lists them in `llm_analysis.rule_filled`.
- `executive_summary`: the three things that matter most for the business, in one short line each.
- `site_specific_findings`: observations the generic checks cannot express, each with evidence (URL, value or exact text). Examples: JavaScript-only counters that ship as "0 +", a heading that spells a name differently from the menu, placeholder content, visible typos, broken `mailto` links, leaked debug headers, structured-data defects, duplicate `-0` CMS pages.
- `quick_wins`: up to 12 actions, ranked by priority and effort, at least 3 of them from the LLM when it produced them.
  - Rule wins name the exact URL, file, value or text involved (e.g. "Add alt text to logo_bio.png in the shared template (missing on 15 of 15 pages)").
  - The summary cuts each win at a clause boundary (about 180 characters); the full text, `priority`, `effort`, `category` and `source` (`rule` or `llm`) stay in the full report.
- `country`: from the TLD, the structured-data address, then phone prefixes; "Not determined" when there is no signal. The matching privacy law (e.g. `.co.id` → UU PDP, `.sg` → PDPA, EU → GDPR) is used by the cookie-banner finding and the LLM prompt.
- `content_seo.entity_rich_content`, `technical_seo.seo_plugin` and `technical_seo.internal_linking` follow the fields of the manual reference reports.

### LLM input and filtering

- The LLM receives today's date, the country, the content language, the privacy law, the sample size, the findings, the pages linked from the homepage (`existing_paths`) and the non-social domains it links to (`related_domains`).
- Its output is checked and corrected (up to two correction rounds for broken JSON, non-English prose or JSON fragments in keyword lists).
- Dropped afterwards:
  - quick wins without `evidence`, or that restate a rule-based win;
  - keywords or wins containing a past year;
  - navigation labels passed off as captured keywords;
  - pages it proposes to create that already exist;
  - keywords/wins for an intent a sister site of the same brand already serves (kimiafarma.co.id → kimiafarmaapotek.co.id owns "apotek").
- An LLM Organization-schema win replaces the generic rule schema win, because the LLM can name the legal entity, ticker and parent company.
- Keyword suggestions are tagged `llm_suggested`: they are hypotheses, not search-volume data.

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

Page selection ranks about / services / pricing / contact / blog / case-study pages first (English and Indonesian URL words; a leading locale segment such as `/id/` or `/en-GB/` is ignored). It caps blog posts at about 40% of slots, reserves 2 slots for blog posts when any exist, and skips login, cart, tag, pagination, file and tracking-parameter URLs. Dead or failing candidates are replaced by the next-ranked URL until `max_pages` usable pages are collected or `max_fetch_attempts` fetches are used. Afterwards, up to `sitemap_probe` extra sitemap URLs are status-checked (not analyzed) to estimate how many are dead.

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
seo/locale.py      content language, country, privacy law
seo/llm.py         Cerebras client, prompt, digest, JSON repair
seo/schema.py      Pydantic models = the JSON contract
seo/storage.py     reports, history, index, crawl cache
seo/pipeline.py    ties it together (used by CLI and API)
```

## Known limits

- Plain HTTP fetching, no JavaScript execution. Server-rendered sites (Webflow, WordPress, Shopify) work fully. Pure SPAs get flagged instead.
- Cookie-banner detection reads static HTML, so banners injected only via GTM can be missed. The note in the report says so.
- No search-volume or ranking data. To add it, connect Google Search Console or a keyword API later.
