"""
Firecrawl connector — deep page/thread indexing for high-risk mentions.

Add to .env:
  FIRECRAWL_API_KEY=fc-...

Use when:
  - A mention score crosses the deep-index threshold (default: impact_score > 55)
  - Legal/medical keywords appear in title or snippet
  - A Reddit thread or article needs full context before AI analysis
  - User manually triggers deep-index from the mention detail page

Fetched content is stored in the mention_context table and displayed on
the mention detail page under "Full Context Available".
"""

import os
import json
from datetime import datetime

import requests

from db import get_db

_API_BASE = "https://api.firecrawl.dev/v1"

# Impact score above which we auto-deep-index during pipeline processing
DEEP_INDEX_THRESHOLD = 55

# Risk bonuses that always trigger deep indexing regardless of score
DEEP_INDEX_KEYWORDS = [
    "lawsuit", "attorney", "lawyer", "malpractice", "court", "sued", "suing",
    "settlement", "death", "died", "infection", "hospitali", "icu", "sepsis",
    "botched", "disfigured", "negligence", "investigation", "medical board",
]


def _api_key() -> str:
    key = os.getenv("FIRECRAWL_API_KEY", "")
    if not key:
        raise ValueError("FIRECRAWL_API_KEY not set in environment")
    return key


def scrape_url(url: str) -> dict:
    """
    Fetch full page content via Firecrawl.
    Returns dict with: success, title, markdown, raw_text, linked_urls.
    """
    r = requests.post(
        f"{_API_BASE}/scrape",
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type":  "application/json",
        },
        json={
            "url":     url,
            "formats": ["markdown", "html"],
            "actions": [],
            "onlyMainContent": True,
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    if not data.get("success"):
        return {"success": False, "error": data.get("error", "Unknown error")}

    page = data.get("data", {})
    md   = page.get("markdown") or ""
    meta = page.get("metadata", {})

    # Extract linked URLs from markdown (simple [text](url) pattern)
    import re
    linked_urls = list(set(re.findall(r"\[(?:[^\]]+)\]\((https?://[^)]+)\)", md)))[:30]

    # Clean text from markdown (strip headers and formatting)
    raw_text = re.sub(r"#+\s*", "", md)                  # strip headers
    raw_text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw_text)  # links → text
    raw_text = re.sub(r"[*_`~>]+", "", raw_text)         # formatting chars
    raw_text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()

    return {
        "success":     True,
        "page_title":  meta.get("title") or meta.get("ogTitle") or "",
        "markdown":    md[:50_000],          # cap at 50k chars
        "raw_text":    raw_text[:30_000],    # cap at 30k chars
        "linked_urls": linked_urls,
    }


def deep_index_mention(mention_id: str) -> bool:
    """
    Fetch full page content for a mention and store in mention_context.
    Returns True on success, False if URL missing, already indexed, or fetch fails.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, url, title, deep_indexed FROM mentions WHERE id=?",
            (mention_id,)
        ).fetchone()
        if not row:
            return False

        url = row["url"]
        if not url:
            print(f"[Firecrawl] Mention {mention_id} has no URL — cannot deep-index")
            return False

        already = conn.execute(
            "SELECT mention_id FROM mention_context WHERE mention_id=?",
            (mention_id,)
        ).fetchone()
        if already:
            return True   # Already indexed

    print(f"[Firecrawl] Deep-indexing {url}")
    try:
        result = scrape_url(url)
    except Exception as e:
        print(f"[Firecrawl] Error scraping {url}: {e}")
        return False

    if not result["success"]:
        print(f"[Firecrawl] Failed: {result.get('error')}")
        return False

    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO mention_context
              (mention_id, page_title, raw_text, markdown, linked_urls, indexed_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
        """, (
            mention_id,
            result["page_title"],
            result["raw_text"],
            result["markdown"],
            json.dumps(result["linked_urls"]),
        ))
        conn.execute(
            "UPDATE mentions SET deep_indexed=1, updated_at=datetime('now') WHERE id=?",
            (mention_id,)
        )
        conn.commit()

    print(f"[Firecrawl] Indexed {len(result['raw_text'])} chars for mention {mention_id}")
    return True


def should_deep_index(mention: dict) -> bool:
    """
    Decide whether a mention warrants deep indexing.
    Called during pipeline processing — cheap, no network call.
    """
    if not mention.get("url"):
        return False

    score = mention.get("impact_score") or 0
    if score >= DEEP_INDEX_THRESHOLD:
        return True

    text = f"{mention.get('title','').lower()} {(mention.get('snippet') or '').lower()}"
    if any(kw in text for kw in DEEP_INDEX_KEYWORDS):
        return True

    return False


def batch_deep_index_high_risk(limit: int = 20) -> dict:
    """
    Find the top-scoring non-indexed mentions with URLs and deep-index them.
    Designed to run as a background job after a scan.
    """
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT m.id, m.url, m.title, m.impact_score
            FROM mentions m
            LEFT JOIN mention_context mc ON m.id = mc.mention_id
            WHERE m.url IS NOT NULL
              AND m.deep_indexed = 0
              AND mc.mention_id IS NULL
              AND m.impact_score >= ?
            ORDER BY m.impact_score DESC
            LIMIT ?
        """, (DEEP_INDEX_THRESHOLD, limit)).fetchall()]

    results = {"indexed": 0, "failed": 0, "skipped": 0}
    for row in rows:
        success = deep_index_mention(row["id"])
        if success:
            results["indexed"] += 1
        else:
            results["failed"] += 1

    print(f"[Firecrawl] Batch complete: {results}")
    return results


def get_mention_context(mention_id: str) -> dict | None:
    """Retrieve stored context for a mention. Returns None if not indexed."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM mention_context WHERE mention_id=?", (mention_id,)
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["linked_urls"] = json.loads(result.get("linked_urls") or "[]")
    except Exception:
        result["linked_urls"] = []
    return result
