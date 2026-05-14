"""
Review Sites connector — extracts patient reviews and ratings from:
  - RealSelf  (plastic surgery reviews)
  - Yelp      (business reviews)
  - BBB       (complaints + ratings)
  - PissedConsumer (complaints)
  - Trustpilot (consumer reviews)
  - Healthgrades (medical reviews)

Method: Google News RSS with site: operator, which bypasses these sites'
anti-scraping protection by indexing through Google's public news index.
No API key required.
"""

import requests
import urllib.parse
import xml.etree.ElementTree as ET
import re
import time

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# Review site definitions: (source_name, display_platform, domain, min_relevance_terms)
REVIEW_SITES = [
    ("realself",         "RealSelf",       "realself.com",        ["realself"]),
    ("bbb",              "BBB",            "bbb.org",             ["bbb", "better business bureau"]),
    ("pissedconsumer",   "PissedConsumer", "pissedconsumer.com",  ["pissed", "goals"]),
    ("healthgrades",     "Healthgrades",   "healthgrades.com",    ["healthgrades"]),
]

# Yelp is handled separately because it returns listing pages, not reviews
YELP_BASE = "yelp.com"

BRAND_TERMS = [
    "goals plastic surgery", "goals aesthetics", "goals surgery",
    "goalsplasticsurgery", "dr. voskin", "dr voskin", "sergey voskin",
    "flexsculpt", "doublebbl", "goals bbl", "goals lipo", "goals ps",
]


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"&nbsp;", " ", text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").strip()


def _parse_pub_date(date_str: str):
    if not date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str).isoformat()
    except Exception:
        return None


def _gnews_site_search(query: str, site: str) -> list:
    """Search Google News RSS restricted to a specific domain."""
    q = f'"{query}" site:{site}'
    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if not r.ok:
            return []
        root = ET.fromstring(r.content)
        return root.findall(".//item")
    except Exception as e:
        print(f"[ReviewSites] Error searching {site}: {e}")
        return []


def _is_brand_relevant(title: str, snippet: str) -> bool:
    text = (title + " " + snippet).lower()
    return any(term in text for term in BRAND_TERMS)


def _item_to_mention(item, source_name: str, platform: str, query: str):
    title   = _clean(item.findtext("title") or "")
    link    = item.findtext("link") or ""
    desc    = _clean(item.findtext("description") or "")
    pub_dt  = _parse_pub_date(item.findtext("pubDate"))

    if not title:
        return None

    return {
        "title":           title,
        "url":             link,
        "snippet":         desc,
        "published_at":    pub_dt,
        "source_name":     source_name,
        "platform":        platform,
        "matched_keyword": query,
    }


def search_review_sites(query: str) -> list:
    """
    Search all major review sites for brand mentions.
    Returns list of mention dicts.
    """
    results = []

    for source_name, platform, domain, _ in REVIEW_SITES:
        items = _gnews_site_search(query, domain)
        for item in items:
            mention = _item_to_mention(item, source_name, platform, query)
            if mention and _is_brand_relevant(mention["title"], mention.get("snippet", "")):
                results.append(mention)
        time.sleep(0.4)

    # Yelp: page-level results (location profile pages, not individual reviews)
    yelp_items = _gnews_site_search(query, YELP_BASE)
    for item in yelp_items:
        mention = _item_to_mention(item, "yelp", "Yelp", query)
        if mention and _is_brand_relevant(mention["title"], mention.get("snippet", "")):
            results.append(mention)
    time.sleep(0.4)

    return results
