"""Classifies responses that are not usable pages.

Policy: we DETECT and RECORD blockers (anti-bot, CAPTCHA, login, geo, age gates). We never try
to bypass them and never impersonate Googlebot.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

ANTI_BOT_MARKERS = (
    "_cf_chl_opt", "cf-browser-verification", "cf_chl_", "<title>just a moment...</title>",
    "attention required! | cloudflare", "checking your browser before accessing",
    "captcha-delivery.com", "/_incapsula_resource", "incapsula incident",
    "request unsuccessful. incapsula", "px-captcha", "perimeterx", "ddos-guard",
    "sucuri website firewall", "<title>access denied</title>",
)
CAPTCHA_MARKERS = (
    "g-recaptcha", "h-captcha", "hcaptcha.com/1/api.js", "cf-turnstile",
    "challenges.cloudflare.com/turnstile", "recaptcha/api.js",
)
GEO_MARKERS = (
    "not available in your country", "not available in your region",
    "unavailable in your region", "not available in your location",
    "not available in your jurisdiction", "access from your country",
)
AGE_GATE_MARKERS = (
    "verify your age", "age verification", "are you over 18", "are you 18", "are you over 21",
    "are you 21", "18 years or older", "21 years or older", "legal drinking age",
    "i am over 18", "i am 18 or older", "enter your date of birth", "you must be 18",
)
JS_APP_MARKERS = (
    '<div id="root"></div>', "<div id='root'></div>", '<div id="app"></div>', 'id="__next"',
    "window.__nuxt__", "ng-version=", "you need to enable javascript",
    "please enable javascript", "enable javascript to run this app",
)
SOFT_404 = re.compile(r"(\b404\b|page not found|not found|doesn.t exist|no longer available)", re.I)
LOGIN_PATH = re.compile(r"/(login|log-in|signin|sign-in|auth|sso|account/login|users/sign_in)(/|$)", re.I)

THIN_WORDS = 150


def _is_login(url: Optional[str]) -> bool:
    return bool(url) and bool(LOGIN_PATH.search(urlparse(url).path))


def has_hard_block_signal(headers: dict, html_lower: str) -> bool:
    """Used to avoid pointless retries on challenge pages."""
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    return any(m in html_lower for m in ANTI_BOT_MARKERS)


def detect_blocker(status: int, headers: dict, html_lower: str, visible_words: int,
                   requested_url: str, final_url: str, redirected: bool = False) -> Optional[str]:
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return "anti_bot"
    if status == 429:
        return "rate_limited"
    thin = visible_words < THIN_WORDS
    if any(m in html_lower for m in ANTI_BOT_MARKERS) and (status in (403, 503) or thin):
        return "anti_bot"
    if thin and any(m in html_lower for m in CAPTCHA_MARKERS):
        return "captcha"
    if status == 401:
        return "auth_required"
    if redirected and _is_login(final_url) and not _is_login(requested_url):
        return "auth_required"
    if status == 451 or (thin and any(m in html_lower for m in GEO_MARKERS)):
        return "geo"
    if status < 400 and visible_words < 80 and any(m in html_lower for m in AGE_GATE_MARKERS):
        return "age_gate"
    if status == 403:
        return "forbidden"
    return None


def page_flags(html_lower: str, visible_words: int, title: str, h1: str) -> list[str]:
    """Non-fatal observations about a usable page."""
    flags = []
    if any(m in html_lower for m in AGE_GATE_MARKERS):
        flags.append("age_gate")
    if visible_words < 60:
        flags.append("needs_js_render" if any(m in html_lower for m in JS_APP_MARKERS) else "thin_html")
    if visible_words < 400 and SOFT_404.search(f"{title} {h1}"):
        flags.append("soft_404")
    return flags
