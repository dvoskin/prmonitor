"""
DuckDuckGo web search connector — completely free, no API key required.
Scrapes DuckDuckGo HTML results for broad web coverage (blogs, review sites, forums).
"""

import requests
import re
from html.parser import HTMLParser


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

SKIP_DOMAINS = {"duckduckgo.com", "duck.com"}


class DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._current = {}
        self._in_title = False
        self._in_snippet = False
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")
        href = attrs_dict.get("href", "")

        if "result__a" in cls:
            self._in_title = True
            if href.startswith("http"):
                self._current["url"] = href
            self._current["title"] = ""

        if "result__snippet" in cls:
            self._in_snippet = True
            self._current["snippet"] = ""

    def handle_endtag(self, tag):
        if self._in_title and tag == "a":
            self._in_title = False
        if self._in_snippet and tag in ("div", "span"):
            self._in_snippet = False
            if self._current.get("title") and self._current.get("url"):
                self.results.append(dict(self._current))
                self._current = {}

    def handle_data(self, data):
        if self._in_title:
            self._current["title"] = (self._current.get("title", "") + data).strip()
        if self._in_snippet:
            self._current["snippet"] = (self._current.get("snippet", "") + data).strip()


def _clean_url(url: str) -> str:
    """Remove DuckDuckGo redirect wrapper if present."""
    if "duckduckgo.com/l/" in url:
        m = re.search(r"uddg=([^&]+)", url)
        if m:
            from urllib.parse import unquote
            return unquote(m.group(1))
    return url


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1).replace("www.", "") if m else ""


def search_duckduckgo(query: str) -> list:
    """
    Search DuckDuckGo for a query. Returns list of mention dicts.
    Free — parses DuckDuckGo's HTML results page.
    """
    try:
        r = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "us-en"},
            headers=HEADERS,
            timeout=12,
        )
        if not r.ok:
            return []

        parser = DDGParser()
        parser.feed(r.text)

        # Fallback regex if parser gets no results
        raw = parser.results
        if not raw:
            titles   = re.findall(r'class="result__a"[^>]*>([^<]+)<', r.text)
            urls     = re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text)
            snippets = re.findall(r'class="result__snippet"[^>]*>([^<]+)<', r.text)
            raw = [
                {"title": t, "url": _clean_url(u), "snippet": s}
                for t, u, s in zip(titles, urls, snippets)
                if t.strip()
            ]

        results = []
        for item in raw:
            url  = _clean_url(item.get("url", ""))
            dom  = _domain(url)
            if dom in SKIP_DOMAINS or not url.startswith("http"):
                continue

            # Determine source type from domain
            platform = _classify_domain(dom)

            results.append({
                "title":           re.sub(r"\s+", " ", item.get("title", "")).strip(),
                "url":             url,
                "snippet":         re.sub(r"\s+", " ", item.get("snippet", "")).strip(),
                "source_name":     "google",   # treated as web search result
                "platform":        platform,
                "matched_keyword": query,
            })

        return results

    except Exception as e:
        print(f"[DuckDuckGo] Error for {query!r}: {e}")
        return []


def _classify_domain(domain: str) -> str:
    mapping = {
        "reddit.com":    "Reddit",
        "yelp.com":      "Yelp",
        "bbb.org":       "BBB",
        "realself.com":  "RealSelf",
        "youtube.com":   "YouTube",
        "tiktok.com":    "TikTok",
        "instagram.com": "Instagram",
        "facebook.com":  "Facebook",
        "twitter.com":   "X / Twitter",
        "x.com":         "X / Twitter",
        "trustpilot.com":"Trustpilot",
        "google.com":    "Google",
        "yelp.ca":       "Yelp",
        "zocdoc.com":    "ZocDoc",
        "healthgrades.com":"Healthgrades",
        "ratemds.com":   "RateMDs",
        "vitals.com":    "Vitals",
    }
    for key, label in mapping.items():
        if key in domain:
            return label
    return f"Web / {domain}"
