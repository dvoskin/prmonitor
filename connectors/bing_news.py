"""
Bing News RSS connector — completely free, no API key required.
Complements Google News RSS with different source coverage.
"""

import requests
import urllib.parse
import xml.etree.ElementTree as ET
import re

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
}


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").strip()


def _parse_pub_date(date_str: str):
    if not date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str).isoformat()
    except Exception:
        return None


def search_bing_news(query: str) -> list:
    """
    Search Bing News RSS for a query. Returns list of mention dicts.
    Free — uses Bing's public news RSS feed.
    """
    # Wrap in quotes for exact phrase matching
    encoded = urllib.parse.quote(f'"{query}"')
    url = f"https://www.bing.com/news/search?q={encoded}&format=RSS"

    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if not r.ok:
            return []

        root = ET.fromstring(r.content)
        results = []

        for item in root.findall(".//item"):
            title    = _clean(item.findtext("title") or "")
            link     = item.findtext("link") or ""
            desc     = _clean(item.findtext("description") or "")
            pub_date = _parse_pub_date(item.findtext("pubDate"))
            source   = _clean(item.findtext("{http://search.yahoo.com/mrss/}provider") or
                               item.findtext("source") or "Bing News")

            if not title:
                continue

            results.append({
                "title":           title,
                "url":             link,
                "snippet":         desc,
                "author":          source,
                "published_at":    pub_date,
                "source_name":     "google_news",   # treated as news
                "platform":        f"Bing News / {source}",
                "matched_keyword": query,
            })

        return results

    except Exception as e:
        print(f"[BingNews] Error for {query!r}: {e}")
        return []
