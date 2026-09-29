"""Content language, country and applicable privacy law. Deterministic, no LLM."""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from .schema import Locale

STOPWORDS = {
    "id": {"yang", "dan", "di", "dari", "untuk", "dengan", "ini", "itu", "dalam", "adalah", "pada", "ke",
           "tidak", "akan", "kami", "atau", "oleh", "juga", "sebagai", "telah", "serta", "melalui"},
    "en": {"the", "and", "of", "to", "in", "is", "for", "with", "that", "on", "are", "as", "by", "this",
           "our", "we", "from", "be", "at", "your"},
    "nl": {"de", "het", "een", "en", "van", "voor", "met", "zijn", "niet", "ook", "wij", "onze"},
    "de": {"der", "die", "das", "und", "mit", "für", "ist", "nicht", "auch", "wir", "unsere", "von"},
    "th": set(),
}
_WORD = re.compile(r"[^\W\d_]+", re.U)
_THAI = re.compile(r"[฀-๿]")

TLD_COUNTRY = {
    "id": "ID", "sg": "SG", "my": "MY", "th": "TH", "ph": "PH", "vn": "VN", "au": "AU", "nz": "NZ",
    "uk": "GB", "in": "IN", "us": "US", "ca": "CA", "br": "BR", "jp": "JP", "kr": "KR", "hk": "HK",
    "de": "EU", "fr": "EU", "nl": "EU", "be": "EU", "es": "EU", "it": "EU", "ie": "EU", "at": "EU",
    "pt": "EU", "pl": "EU", "se": "EU", "dk": "EU", "fi": "EU", "eu": "EU",
}
LANGUAGE_COUNTRY = {"id": "ID", "th": "TH"}
PRIVACY_LAW = {
    "ID": "UU PDP (Law No. 27/2022)", "SG": "PDPA (Singapore)", "MY": "PDPA 2010 (Malaysia)",
    "TH": "PDPA (Thailand)", "PH": "Data Privacy Act 2012", "VN": "Decree 13/2023 on personal data",
    "AU": "Privacy Act 1988", "NZ": "Privacy Act 2020", "GB": "UK GDPR / PECR", "EU": "GDPR / ePrivacy",
    "IN": "DPDP Act 2023", "US": "CCPA/CPRA and state privacy laws", "CA": "PIPEDA", "BR": "LGPD",
    "JP": "APPI", "KR": "PIPA", "HK": "PDPO",
}
GENERIC_LAW = "applicable privacy law (e.g. GDPR)"


_SCRIPTS = (("th", _THAI), ("ru", re.compile(r"[Ѐ-ӿ]")), ("zh", re.compile(r"[一-鿿]")),
            ("ja", re.compile(r"[぀-ヿ]")), ("ko", re.compile(r"[가-힯]")),
            ("ar", re.compile(r"[؀-ۿ]")), ("he", re.compile(r"[֐-׿]")))


def detect_language(text: str) -> Optional[str]:
    """ISO 639-1 code of the dominant language, or None when the sample is too small or ambiguous.

    Non-Latin scripts are recognised by character share (Cyrillic -> 'ru', Han -> 'zh', ...); an English
    menu around Russian copy must not make the page look English."""
    letters = len(_WORD.findall(text))
    for code, rx in _SCRIPTS:
        chars = len(rx.findall(text))
        if chars > 50 and chars > 0.3 * len(re.findall(r"[^\W\d_]", text)):
            return "ja" if code == "zh" and len(_SCRIPTS[3][1].findall(text)) > 20 else code
    if letters and len(_THAI.findall(text)) > 50:
        return "th"
    words = [w.lower() for w in _WORD.findall(text)]
    if len(words) < 40:
        return None
    hits = Counter()
    for w in words:
        for lang, stop in STOPWORDS.items():
            if w in stop:
                hits[lang] += 1
    if not hits:
        return None
    (best, n), *rest = hits.most_common(2) + [("", 0)]
    runner_up = rest[0][1]
    return best if n >= 8 and n >= 1.5 * runner_up else None


def primary(lang: Optional[str]) -> Optional[str]:
    return lang.split("-")[0].split("_")[0].lower() if lang else None


COUNTRY_NAMES = {
    "indonesia": "ID", "singapore": "SG", "malaysia": "MY", "thailand": "TH", "philippines": "PH", "vietnam": "VN",
    "viet nam": "VN", "australia": "AU", "new zealand": "NZ", "united kingdom": "GB", "uk": "GB", "india": "IN",
    "united states": "US", "united states of america": "US", "usa": "US", "canada": "CA", "brazil": "BR",
    "japan": "JP", "south korea": "KR", "korea": "KR", "hong kong": "HK", "germany": "EU", "france": "EU",
    "netherlands": "EU", "belgium": "EU", "spain": "EU", "italy": "EU", "ireland": "EU",
}


def _country_code(value: Optional[str]) -> Optional[str]:
    """'SG', 'sg', 'Singapore' -> 'SG' (EU members -> 'EU'). None when unknown."""
    if not value:
        return None
    v = value.strip().lower()
    if len(v) == 2:
        return TLD_COUNTRY.get(v, v.upper() if v.upper() in PRIVACY_LAW else None) or (v.upper() if v != "uk" else "GB")
    return COUNTRY_NAMES.get(v)


def site_locale(domain: str, html_lang: Optional[str], content_languages: list[Optional[str]],
                home_content_language: Optional[str], org_country: Optional[str] = None,
                phone_country: Optional[str] = None) -> Locale:
    langs = Counter(l for l in content_languages if l)
    content_lang = home_content_language or (langs.most_common(1)[0][0] if langs else None)
    tld = domain.rsplit(".", 1)[-1].lower() if domain else ""
    country, source = TLD_COUNTRY.get(tld), "tld"
    if country is None and _country_code(org_country):
        country, source = _country_code(org_country), "structured_data"
    if country is None and _country_code(phone_country):
        country, source = _country_code(phone_country), "phone"
    if country is None:
        country, source = LANGUAGE_COUNTRY.get(content_lang or ""), "content_language"
    return Locale(country=country, country_source=source if country else None, html_lang=html_lang,
                  content_language=content_lang, privacy_law=PRIVACY_LAW.get(country or "", GENERIC_LAW))
