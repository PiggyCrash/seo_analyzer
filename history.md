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

## 2026-09-29: Four-site benchmark (SUN Energy, Flex, Schneider Electric, McDermott) and Groq vs MiniMax

### Requirements

- Run the analyzer on sunenergy.id, flex.com, se.com, mcdermott.com; compare with an independent manual crawl and with the coworker's quality bar; improve the code; compare Groq with MiniMax

### Problems found and solutions applied

| Problem | Solution |
|---|---|
| se.com LLM call failed (413): robots.txt details carried 523 sitemap URLs into the digest | Digest lists/strings are bounded (`_slim`), hard size fallback; Groq `max_digest_chars` 10000 |
| First 3 robots.txt sitemaps analysed (a UAE downloads sitemap on se.com) | Sitemaps ranked by the homepage's path prefix and content type (`rank_sitemaps`); declared count reported |
| hreflang "missing" on se.com although the sitemaps carry 14k annotations | Sitemap hreflang annotations counted; hreflang present when in the sitemap |
| "10 of 10 sampled URLs redirect" caused by our own URL normalisation | Sitemap URLs probed exactly as listed |
| Tracking inside GTM invisible ("GTM" only) | Public GTM container read once: GA4, Google Ads, Meta Pixel, LinkedIn, TikTok, Hotjar, Clarity IDs, dead UA tags; GTM environment snippet flagged |
| Name-mismatch false positives ("PLTS atau" vs "PLTS Atap", "lend" vs "led") | Only capitalised words against short menu labels |
| LLM returned broken JSON / Indonesian prose / JSON fragments as keywords | Output validation with up to two correction rounds, tolerant JSON extraction, junk keywords stripped |
| LLM proposed pages that already exist, invented subsidiaries as competitors | `existing_paths` in the digest + deterministic filter; prompt rules |
| Missed site-specific defects | New findings: duplicate `-0` CMS pages (fetched and compared), Drupal hidden block titles as H2, generic "Home" title, template brand suffix pushing titles over 60, FAQ content without FAQPage, Organization without sameAs/logo/address, empty FAQPage, dev URL leaked in JSON-LD, author "Admin", relative logo, http Organization URL, invalid geo, zoom-blocking viewport, malformed numbers ("150,0000"), browser-extension attributes, empty/space-padded hrefs, malformed meta names, reused og:image, end-of-life server software, dead child sitemaps, truncated slugs, obsolete sitemap namespace, homepage missing from sitemap |
| Platform "Not detected" / bare "PHP/8.1" | Builder.io, AEM, Sitecore, Laravel (XSRF cookie); hosting (Pantheon, Akamai, Cloudflare, Fastly, CloudFront, ...) and WordPress plugins (Rank Math, Yoast, TranslatePress, ...) |
| Company / country null without the LLM | Company from Organization JSON-LD, og:site_name or title suffix; country from JSON-LD addressCountry |
| Soft-404 via redirect described as "returns 200" | Probe records the redirect target |

### Groq (gpt-oss-120b) vs MiniMax (MiniMax-M3), same crawl

MiniMax-M3 writes site-specific analysis (named projects, products, executives, real competitors such as Xurya for SUN Energy, Subsea7/Saipem for McDermott) and keywords that match the business model ("PLTS tanpa modal", "skema PPA"). Groq gives generic CTA/FAQ advice and wrong competitors (Adaro, Pertamina for a C&I solar developer). MiniMax is slower (about 1 min per site) and sometimes needs a JSON correction round. Run it with `--config config.minimax.yaml`.

## 2026-09-29: No empty fields, internal linking, findings in the score

### Requirements

- No null or empty values in the summary (sector, captured_keywords, missed_keywords, ...), fields like the coworker's example
- Add an internal-linking check and let serious findings lower the rating

### Problems found and solutions applied

| Problem | Solution |
|---|---|
| LLM fields empty whenever the LLM failed or was skipped | `llm.fallback` provider in config (Groq -> MiniMax, MiniMax -> Groq); if both fail, `seo/fallback.py` fills sector, keyword strategy, captured/missed keywords, competitive position and summary from measured facts, marked "(rule-based ...)" and listed in `llm_analysis.rule_filled` |
| `country` null on .com sites | Country from JSON-LD address, then tel:/WhatsApp phone prefixes; otherwise an explicit "Not determined ..." |
| `company` null | Organization JSON-LD, og:site_name, title prefix/suffix, homepage title matching the domain, then the domain |
| Blocked sites: empty keywords, caveats, quick wins | Brand-only keywords labelled unverified, a score caveat, and a "make the site readable to crawlers" win |
| Empty `site_specific_findings` | States what was checked and that nothing was found |
| No internal-linking analysis | `internal_linking` check: card grids whose "Read More" links all point to one listing page, sampled pages nothing links to, generic-anchor share, internal nofollow |
| Score 84 "Good" with lorem ipsum and a typo live | Score = technical hygiene minus 5 per high and 2 per medium finding (max 20); `technical_value` and `penalties` kept, caveat explains it |
| Example fields missing | `technical_seo.internal_linking`, `technical_seo.seo_plugin`, `content_seo.entity_rich_content` |

## 2026-09-29: Content-quality score, executive summary, MiniMax as default

### Requirements

- Score closer to an expert's verdict (content gaps, not only technical hygiene)
- Easier to read: a three-line executive summary, shorter quick wins
- MiniMax-M3 as the priority model, Groq as fallback
- README output section as bullet points, history in chronological order

### Problems found and solutions applied

| Problem | Solution |
|---|---|
| 1Solar scored 71 "Good" with placeholder pages and thin content | Content deductions: thin pages -4/-6, under 250 words per page -3, no service/product/case-study pages -3, no blog/news -2, empty template pages -3 (max -12; total with findings max -25). The caveat names each deduction |
| Reports long, no "what matters most" | `executive_summary`: 3 bullets written by the LLM (rule-based fallback: top 3 ranked quick wins) |
| Quick wins up to 620 characters | Summary cuts each win at a clause boundary (about 180 characters); the full text stays in `output/history/` |
| Groq first, weaker analysis | `config.yaml` = MiniMax-M3 with Groq fallback; `config.groq.yaml` keeps the old order |

### Results (same cached crawls, 8 sites)

| Site | Earliest | v1.4 | v1.5 | Coworker verdict |
|---|---|---|---|---|
| kimiafarma.co.id | 68 | 55 | 46 Weak | — |
| sunenergy.id | 83 | 72 | 70 Good | — |
| flex.com | 87 | 79 | 77 Good | — |
| se.com | 82 | 79 | 74 Good | — |
| mcdermott.com | 66 | 61 | 59 Fair | — |
| zeropenny.co | — | 68 | 66 Fair | Weak |
| 1achieve.net | — | 71 | 65 Fair | Poor-to-Fair |
| 1solar.sg | — | 71 | 69 Fair | Moderate |

- Longest quick win: 620 characters (v1.4) → 180 (v1.5); empty fields: up to 6 per report (earliest) → 0.
- Executive summary: absent before, 3 bullets on every site in v1.5.

### Known issues

- McDermott loses 2 points for "no blog/news" although it publishes press releases under `/press-release-detail/`; that path is not yet recognised as news.
- MiniMax placed 1Solar's "Read More" cards on /projects/ instead of the homepage in one executive-summary bullet.
