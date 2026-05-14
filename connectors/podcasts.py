"""
Podcast connector — finds podcast episodes mentioning Goals Plastic Surgery.

Sources:
  1. iTunes/Apple Podcasts Search API (free, no key required)
     Returns episode-level results with show name, date, and description.
  2. Google News RSS restricted to podcast directories
     Catches episodes indexed by Google Podcasts/Spotify/Podchaser.

Apple's API returns up to 200 results per search with full metadata.
"""

import requests
import urllib.parse
import xml.etree.ElementTree as ET
import re
import time

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

BRAND_TERMS = [
    "goals plastic surgery", "goals aesthetics", "goals surgery",
    "goalsplasticsurgery", "dr. voskin", "dr voskin", "sergey voskin",
    "flexsculpt", "doublebbl", "goals bbl", "goals lipo", "goals ps",
]


def _is_brand_relevant(title: str, snippet: str) -> bool:
    text = (title + " " + snippet).lower()
    return any(term in text for term in BRAND_TERMS)


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def _search_itunes(query: str) -> list:
    """Search Apple Podcasts / iTunes for episodes mentioning the query."""
    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term":   query,
                "media":  "podcast",
                "entity": "podcastEpisode",
                "limit":  "50",
                "country": "us",
            },
            headers=HEADERS,
            timeout=12,
        )
        if not r.ok:
            return []

        data = r.json()
        results = []

        for ep in data.get("results", []):
            title       = ep.get("trackName", "")
            show        = ep.get("collectionName", "")
            episode_url = ep.get("trackViewUrl") or ep.get("episodeUrl") or ""
            description = _clean(ep.get("description") or ep.get("shortDescription") or "")
            pub_date    = ep.get("releaseDate", "")[:10]  # YYYY-MM-DD
            duration_ms = ep.get("trackTimeMillis", 0)

            if not title:
                continue

            snippet = f"Podcast: {show}. {description[:300]}" if description else f"Podcast: {show}"

            results.append({
                "title":           f"{title}",
                "url":             episode_url,
                "snippet":         snippet,
                "author":          show,
                "published_at":    pub_date if pub_date else None,
                "engagement_count": round(duration_ms / 60000) if duration_ms else None,
                "engagement_label": "min runtime",
                "source_name":     "podcast",
                "platform":        f"Podcast / {show}",
                "matched_keyword": query,
            })

        return results

    except Exception as e:
        print(f"[Podcasts] iTunes error: {e}")
        return []


def _search_gnews_podcasts(query: str) -> list:
    """Use Google News RSS to find podcast coverage on major directories."""
    podcast_sites = ["podcasts.apple.com", "open.spotify.com", "podchaser.com"]
    results = []

    for site in podcast_sites:
        try:
            q = f'"{query}" site:{site}'
            url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if not r.ok:
                continue
            root = ET.fromstring(r.content)
            for item in root.findall(".//item"):
                title = _clean(item.findtext("title") or "")
                link  = item.findtext("link") or ""
                desc  = _clean(item.findtext("description") or "")
                pub   = item.findtext("pubDate")
                try:
                    from email.utils import parsedate_to_datetime
                    pub_iso = parsedate_to_datetime(pub).isoformat() if pub else None
                except Exception:
                    pub_iso = None

                if not title:
                    continue

                platform_map = {
                    "podcasts.apple.com": "Apple Podcasts",
                    "open.spotify.com": "Spotify",
                    "podchaser.com": "Podchaser",
                }
                results.append({
                    "title":           title,
                    "url":             link.replace("/rss/articles/", "/articles/") if "news.google.com/rss/articles/" in link else link,
                    "snippet":         desc,
                    "published_at":    pub_iso,
                    "source_name":     "podcast",
                    "platform":        platform_map.get(site, "Podcast"),
                    "matched_keyword": query,
                })
            time.sleep(0.3)
        except Exception:
            pass

    return results


def search_podcasts(query: str) -> list:
    """
    Search for podcast episodes mentioning the query.
    Combines iTunes API + Google News RSS for podcast directories.
    """
    results = []

    # iTunes — direct, no key needed, best data quality
    itunes = _search_itunes(query)
    results.extend(itunes)

    # Google News for Spotify / Apple Podcasts directory pages
    gnews = _search_gnews_podcasts(query)
    results.extend(gnews)

    # Apply brand filter
    return [r for r in results if _is_brand_relevant(r.get("title", ""), r.get("snippet", ""))]
