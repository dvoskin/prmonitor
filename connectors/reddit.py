"""
Reddit connector — searches Reddit for brand mentions.
Uses the public JSON API (no key required).
Searches both with exact quoted queries and in targeted subreddits.
"""

import os
import requests
import time
from datetime import datetime

HEADERS = {
    "User-Agent": os.getenv("REDDIT_USER_AGENT", "GoalsPRMonitor/1.0 (reputation monitoring)")
}

# Subreddits where plastic surgery is discussed
TARGET_SUBREDDITS = ["PlasticSurgery", "BBL", "cosmeticsurgery", "PlasticSurgeryQA",
                     "MedicalMalpractice", "legaladvice"]

# Brand relevance — at least one must appear in text for result to be kept
BRAND_TERMS = [
    "goals plastic surgery", "goals aesthetics", "goals surgery",
    "dr. voskin", "dr voskin", "sergey voskin",
    "flexsculpt", "doublebbl", "goals bbl", "goals lipo",
    "goals ps", "goalsplasticsurgery",
    # Partial — "goals" alone if in a surgery subreddit context
]


def _is_relevant(title: str, snippet: str) -> bool:
    text = (title + " " + snippet).lower()
    return any(term in text for term in BRAND_TERMS)


def _to_mention(p: dict, query: str):
    title    = p.get("title", "")
    selftext = (p.get("selftext") or "")[:600]
    snippet  = selftext if selftext.strip() else f"Posted in r/{p.get('subreddit', 'reddit')}"

    if not _is_relevant(title, snippet):
        return None

    pub_ts = p.get("created_utc", 0)
    pub_dt = datetime.utcfromtimestamp(pub_ts).isoformat() if pub_ts else None

    return {
        "title":            title,
        "url":              f"https://reddit.com{p.get('permalink', '')}",
        "snippet":          snippet,
        "author":           f"u/{p.get('author', 'unknown')}",
        "published_at":     pub_dt,
        "engagement_count": p.get("score", 0),
        "engagement_label": "upvotes",
        "source_name":      "reddit",
        "platform":         f"Reddit r/{p.get('subreddit', 'reddit')}",
        "matched_keyword":  query,
    }


def _fetch(url: str, params: dict) -> list:
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=12)
        if not r.ok:
            return []
        return r.json().get("data", {}).get("children", [])
    except Exception as e:
        print(f"[Reddit] Fetch error {url}: {e}")
        return []


def search_reddit(query: str) -> list:
    """
    Search Reddit for brand-relevant mentions.
    Uses quoted exact search + targeted subreddit search.
    """
    children = []

    # 1. Exact quoted search across all Reddit
    quoted = f'"{query}"'
    children += _fetch("https://www.reddit.com/search.json",
                       {"q": quoted, "sort": "new", "limit": 25, "t": "all"})
    time.sleep(0.5)

    # 2. Unquoted search in targeted subreddits (catches posts that mention brand in comments)
    if any(kw in query.lower() for kw in ["goals", "voskin", "flexsculpt", "doublebbl"]):
        for sr in TARGET_SUBREDDITS[:2]:  # limit to 2 subreddits for speed
            children += _fetch(
                f"https://www.reddit.com/r/{sr}/search.json",
                {"q": query, "restrict_sr": "on", "sort": "new", "limit": 10, "t": "all"}
            )
            time.sleep(0.4)

    # 3. Deduplicate by post ID and filter for brand relevance
    seen = set()
    results = []
    for child in children:
        p = child.get("data", {})
        pid = p.get("id", "")
        if pid in seen:
            continue
        seen.add(pid)
        mention = _to_mention(p, query)
        if mention:
            results.append(mention)

    return results
