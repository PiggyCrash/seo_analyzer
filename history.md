# History

## 2026-09-28: Site-specific depth (comparison with a coworker's reports)

### Requirements

- Bring the Kimia Farma report to the quality of the coworker's reports (1solar.sg, 1achieve.net, 0penny.com): site-specific insight, measured data that holds up against a manual crawl, depth, no internal contradictions
- Remove `larger_projects` from the summary; quick wins as plain action strings, like the coworker's JSON

### Problems found and solutions applied

| Problem | Cause | Solution |
|---|---|---|
| "1 page without H1" when 5 had none | Empty `<h1></h1>` counted as an H1 | Missing = no non-empty H1; `empty_tag` count and examples; homepage top heading reported |
| "Content is rich (avg 570 words)" | Word count taken before menus were removed, and this site has no `<nav>` tags; the mega-menu renders twice | Text repeated on ≥50% of pages is removed before word counts and excerpts |
| 4 of 15 pages were empty CMS shells, analyzed as content | No soft-404 detection (`/id/anything` returns 200) | Crawler probes a nonexistent URL; `not_found_handling` check (weight 6); `empty_template` pages excluded from content stats |
| "Fix broken links (10 URLs 404)" | Dead sitemap URLs reported as broken links | Sitemap prefix diagnosis: 193 entries use `/en-GB/` `/id-ID/`, the same slugs work under `/en/` `/id/`; plus duplicate and malformed entries |
| og tags "Present" | Only checked that tags exist | Flags og:url pointing to the homepage on inner pages, constant og:title, og:site_name holding a URL |
| "15 of 84 images missing alt" | Counted per image | Detects one image missing on most pages (logo_bio.png, a shared-template fix) |
| Platform "Not detected" | HTML markers only | Session cookies (ci_session → CodeIgniter, laravel_session, PHPSESSID...) |
| Analytics "Meta Pixel" | No per-page coverage | Reports inner-pages-only trackers and missing GA4/GTM |
| No hreflang finding | hreflang "not scored" | Detects `/id/` + `/en/` trees without hreflang, merged into the lang quick win |
| No site-specific insight | Nothing looked for anomalies | `site_specific_findings`: "0 +" JS counters, heading vs. menu spelling (MARIN LISA vs Marin Liza), `X-Debug-HTACCESS` header, placeholder content, stale copyright, phone number in an H6, 27 H5/H6 layout labels, empty `google-site-verification` / `fb:app_id` |
| Generic quick wins | Rule wins stated counts only | Each rule win names the URL, value or file; findings become wins |
| Captured keywords were menu labels | LLM excerpts started with the menu | Boilerplate-free excerpts; nav labels and ALL-CAPS labels dropped |
| Retail keywords on a corporate site | LLM ignored the subsidiary domains | `related_domains` in the digest; keywords/wins for sister-site intents (apotek, lab, eproc) dropped |
| LLM wins repeated rule wins | Filter only matched each rule's own check; a non-breaking hyphen ("JSON‑LD") slipped past | Filter matches every topic a rule win mentions; unicode dashes normalized |
| `larger_projects` | Separate list | Removed; `quick_wins` is a list of up to 12 action strings |

### Kimia Farma result

| | Before | After |
|---|---|---|
| Score | 66 Fair | 59 Fair (soft-404 now scored) |
| Soft-404 | Not checked | CRITICAL: `/id/<anything>` returns 200 |
| Sitemap | "Present (267 URLs)" | 263 entries, 217 unique; 193 on wrong locale prefixes; 18 malformed; 46 duplicates |
| H1 | 1 page without | 6 without, 4 of them empty `<h1>` |
| Avg words | 570 | 290 of page-specific text, on 11 real pages |
| Site-specific findings | 0 | 8 |

Schema version 1.2, tool version 1.2.0. Old reports still load in the summary.

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
