"""Live trend terms: Google Trends RSS -> plain search terms.

The IG Graph API exposes no trending-hashtag feed on Instagram Login
(ig_hashtag_search 400s, subcode 33), so "current trends" come from Google
Trends' public daily-search RSS — no key, no login, no scraping of IG.

These are raw search queries, not hashtags. src/ai.py maps them onto the
account's niche, which is the only honest way to get a #tag from a search
term: the model filters, it never invents a "trend" it wasn't shown.

Never raises: any failure returns [] and the caller keeps its curated pool.
"""
import os
import xml.etree.ElementTree as ET

import requests

RSS_URL = "https://trends.google.com/trending/rss"
GEO = os.environ.get("TREND_GEO", "IN")  # account's market
LIMIT = 20
TIMEOUT = 15


def _parse(xml_text: str, limit: int = LIMIT) -> list[str]:
    """<item><title> values, lowercased and deduped, newest first."""
    root = ET.fromstring(xml_text)
    out: list[str] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip().lower()
        if title and title not in seen:
            seen.add(title)
            out.append(title)
        if len(out) >= limit:
            break
    return out


def live_terms(geo: str | None = None, limit: int = LIMIT) -> list[str]:
    """Today's trending searches for a region. [] on any failure."""
    try:
        r = requests.get(RSS_URL, params={"geo": geo or GEO}, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        return _parse(r.text, limit)
    except Exception:
        return []
