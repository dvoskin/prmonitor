"""
Google News RSS connector — completely free, no API key required.
Returns up to 100 results per keyword from Google's public news RSS feed.
"""

import requests
import urllib.parse
import xml.etree.ElementTree as ET
import re
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def _clean(text: str) -> str:
    """Strip HTML entities and tags."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return text.strip()


def _parse_pub_date(date_str: str):
    """Parse RFC 2822 date from RSS."""
    if not date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str).isoformat()
    except Exception:
        return None


def _fix_gnews_url(url: str) -> str:
    """Convert Google News RSS redirect URLs to browser-friendly article URLs.

    RSS feeds return /rss/articles/CBMi... URLs which don't redirect in browsers.
    The /articles/CBMi... format opens correctly in Chrome/Firefox with JS redirect.
    """
    if url and "news.google.com/rss/articles/" in url:
        return url.replace("/rss/articles/", "/articles/")
    return url


def search_google_news_rss(query: str) -> list:
    """
    Search Google News RSS for a query. Returns list of mention dicts.
    Free — uses Google's public RSS endpoint.
    """
    # Wrap in quotes for exact phrase matching — prevents "Goals" matching soccer articles
    encoded = urllib.parse.quote(f'"{query}"')
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"

    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if not r.ok:
            return []

        root = ET.fromstring(r.content)
        results = []

        for item in root.findall(".//item"):
            title     = _clean(item.findtext("title") or "")
            link      = item.findtext("link") or ""
            desc      = _clean(item.findtext("description") or "")
            pub_date  = _parse_pub_date(item.findtext("pubDate"))
            source_el = item.find("source")
            source_name_raw = _clean(source_el.text if source_el is not None else "Google News")

            if not title:
                continue

            results.append({
                "title":           title,
                "url":             _fix_gnews_url(link),
                "snippet":         desc,
                "author":          source_name_raw,
                "published_at":    pub_date,
                "source_name":     "google_news",
                "platform":        f"Google News / {source_name_raw}",
                "matched_keyword": query,
            })

        return results

    except Exception as e:
        print(f"[GoogleNewsRSS] Error for {query!r}: {e}")
        return []
