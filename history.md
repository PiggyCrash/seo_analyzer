# History

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
