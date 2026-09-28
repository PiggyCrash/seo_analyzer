# History

## 2026-09-28: Interpretation-layer fixes (Kimia Farma review)

### Requirements

- Fix the review points on the Kimia Farma report: 3-page sample, BlogPosting win with no blog, a 68 score that looked too high, PDPA on an Indonesian site, a "2024" keyword in a 2026 report, generic and unranked quick wins
- Keep the same model (`openai/gpt-oss-120b` on Groq)

### Problems found and solutions applied

| Problem | Cause | Solution |
|---|---|---|
| Only 3 of 15 pages analyzed | `classify_path` matched words anywhere in the URL, so dead sitemap news slugs (`/en-GB/read/company-...`, `...-products-...`) ranked as about/service pages above ~80 real homepage links | Ignore locale prefixes, detect `/read/`, `/berita/` etc. as news, add Indonesian page words, replace dead URLs with the next candidate (`max_fetch_attempts`) |
| Dead sitemap URLs not reported | No check for them | New `broken_links` check (weight 5) + sitemap probe (`sitemap_probe`) + rule quick win |
| BlogPosting schema recommended with no blog | Fixed rule text | Only mentions blog posts when a blog is detected |
| PDPA cited for Indonesia | Hard-coded "PDPA / GDPR" | `seo/locale.py` picks the law by country (ID → UU PDP) |
| `lang="en"` on Indonesian content marked OK | No content-language check | Stopword language detection; `lang` becomes partial on mismatch + rule win |
| "vaksin ... 2024" keyword | Prompt had no date/country | Digest `context` with date, country, language, privacy law, sample size; post-filter drops past-year keywords |
| Generic filler wins | No grounding requirement | Wins must include `evidence`; wins without it or repeating a rule are dropped |
| Big projects next to 5-minute fixes | Summary kept only the action text | Summary quick wins keep priority/effort/category/source; high-effort items go to `larger_projects` |
| Score looked authoritative on 3 pages | No confidence | `score.confidence`, `pages_scored`, `caveats`; LOW CONFIDENCE in `overall_rating` |
| LLM wrote the report in Indonesian | "write for the audience's language" | Prose in English, keywords in the audience's language |
| Awards page classified as pricing | "harga" matched inside "penghargaan" | Removed "harga" |

### Kimia Farma result

| | Before | After |
|---|---|---|
| Pages analyzed | 3 | 15 (+1 blog post) |
| Score | 68 Fair, no confidence | 66 Fair, high confidence |
| Dead URLs | Not reported | 10 of 17 sitemap URLs checked return 404 |
| Privacy law | PDPA / GDPR | UU PDP (Law No. 27/2022) |
| Lang | "present" | Mismatch: declared `en`, content `id` |

Schema version 1.1, tool version 1.1.0. Old reports still load in the summary.

## 2026-09-27: First test run, LLM provider switch, quota notice

### Requirements

- Test whether the project (fresh from Claude, unedited) runs
- Use a MiniMax API key instead of Cerebras
- Show a notification when the API key is exhausted
- Save results as JSON
- Use a free option when MiniMax is unavailable, so Groq's free tier
- Run on a real site: https://www.dexagroup.com/id/
- Remove all comments from config, YAML and code
- Keep `output/` out of git

### Problems found and solutions applied

| Problem | Solution | Result |
|---|---|---|
| WSL had no pip or venv | Used standalone `uv` in a temp folder to install dependencies | Worked |
| MiniMax key rejected on the China endpoint (`api.minimaxi.com`) | Switched to the international endpoint `api.minimax.io` | Worked |
| MiniMax key quota used up ("Token Plan usage limit reached (2056)") | Switched to the Groq free tier (`openai/gpt-oss-120b`) | Worked |
| Quota errors were retried pointlessly | Errors are sorted into quota / auth / temporary. Quota and auth errors fail at once, and the LLM is skipped for `quota_cooldown_seconds` | Worked (tested) |
| No notice when the key runs out | CLI `QUOTA EXHAUSTED` / `KEY REJECTED` notice, API `warning` field, `/health` status, and `llm_analysis.error_type` in the JSON | Worked (tested) |
| MiniMax `<think>` blocks would break JSON parsing | Strip `<think>...</think>` before parsing | Worked (tested with a mock API) |
| MiniMax replies could be cut off | Raised `max_tokens` from 2500 to 8000 for MiniMax | Not verified live (quota used up) |
| Groq's daily limit was treated as temporary ("try again in 7m") | Check per-minute limits first, then daily/plan limits | Worked (tested) |
| Groq free tier allows 8k tokens per minute | `max_tokens` 3500, `max_digest_chars` 12000, `requests_per_minute` 2 | Worked |
| The `--reanalyze` hint used an invalid site key | The hint now uses the URL you typed | Worked |
| dexagroup.com returns 403 to the crawler's User-Agent | Tested on sites that allow it (kalbe.co.id, kimiafarma.co.id). Not bypassed | Worked |

### Test results

| Site | Result |
|---|---|
| Local test site (`tests/fixture_site.py`) | Crawl, blocker detection, score 58, LLM OK |
| dexagroup.com | Blocked (403 User-Agent filter), score n/a |
| kalbe.co.id | Score 62, LLM OK. JavaScript-only site, so a shallow result |
| kimiafarma.co.id | Score 68, LLM OK, 14 quick wins. 12 of 15 pages were dead sitemap links (404) |

### Known limitations and possible solutions

| Limitation | Possible solution |
|---|---|
| Sites that block crawler User-Agents (dexagroup.com) | Site allowlists `SEOAnalyzerBot`, or use a browser User-Agent with permission |
| JavaScript-only sites give shallow results (kalbe.co.id) | Add a headless browser (Playwright) |
| Broken sitemap links are not a quick win, and the LLM misreads them | Add a dead-link rule |
| Input path is ignored (`/id/` becomes the homepage) | Add an option to start the crawl from the given path |
| Cloudflare, CAPTCHA and anti-bot pages stop the crawl | By design, no bypass |
| Login-only or geo-blocked pages are skipped | By design |
| Groq free tier: about 1 site/minute for the LLM step, plus a daily cap | Paid tier, or local Ollama |
| Sites are saved without LLM analysis when the daily limit is reached | Run `--reanalyze` later |
| A cached report is reused for 24h, even when its LLM step failed | `--force` or `--reanalyze` |
| LLM keywords are guesses, with no search-volume data | Connect an SEO data API |
| Free/open models are weaker than paid ones | Paid model (e.g. Claude via the Anthropic SDK) |
| User-Agent contact email is still `you@example.com` | Set a real email in `config.yaml` |
| A successful MiniMax reply was never tested live | Re-test once the quota resets |
| Claude isn't supported (a Claude.ai login is not an API key) | Add the Anthropic SDK and use an API key from console.anthropic.com |
| API keys were pasted in chat | Replace them in the MiniMax and Groq dashboards |
| Only tested in WSL | Test in Windows PowerShell |
| No automated tests | Add pytest tests around `tests/fixture_site.py` |
