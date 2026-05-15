"""
Apify connector — high-speed social parsing for TikTok, Instagram, Reddit, YouTube, X/Twitter.

Add to .env:
  APIFY_API_TOKEN=apify_api_...

Recommended actors per platform:
  TikTok:    clockworks/free-tiktok-scraper    (free tier: ~1k results/run)
  Instagram: apify/instagram-hashtag-scraper
  Reddit:    trudax/reddit-scraper
  YouTube:   streamers/youtube-scraper
  X/Twitter: quacker/twitter-scraper           (rate-limited on free tier)
  Facebook:  apify/facebook-pages-scraper      (requires Facebook login)

Actors are configured per-keyword via the APIFY_ACTOR_MAP in this file
or overridden via the 'config' JSON column on the integrations table.
"""

import os
import uuid
import json
import time
from datetime import datetime

import requests

from db import get_db

_API_BASE = "https://api.apify.com/v2"

# Default actor map — override in integrations.config JSON
APIFY_ACTOR_MAP = {
    "tiktok":    "clockworks/free-tiktok-scraper",
    "instagram": "apify/instagram-hashtag-scraper",
    "reddit":    "trudax/reddit-scraper",
    "youtube":   "streamers/youtube-scraper",
    "twitter":   "quacker/twitter-scraper",
    "facebook":  "apify/facebook-pages-scraper",
}

# Default search inputs per platform
def _default_input(platform: str, keyword: str) -> dict:
    p = platform.lower()
    if p == "tiktok":
        return {"keywords": [keyword], "resultsPerPage": 30, "maxItems": 60}
    elif p == "instagram":
        return {"hashtags": [keyword.replace(" ", "").lower()], "resultsLimit": 30}
    elif p == "reddit":
        return {"searches": [keyword], "maxItems": 30, "sort": "relevance", "time": "week"}
    elif p == "youtube":
        return {"searchKeywords": keyword, "maxResults": 20}
    elif p in ("twitter", "x"):
        return {"searchTerms": [keyword], "maxItems": 20, "sort": "Latest"}
    else:
        return {"query": keyword, "maxItems": 30}


def _token() -> str:
    tok = os.getenv("APIFY_API_TOKEN", "")
    if not tok:
        raise ValueError("APIFY_API_TOKEN not set in environment")
    return tok


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


# ── Job management ────────────────────────────────────────────────────────────

def start_actor_run(platform: str, keyword: str, actor_id: str = None) -> dict:
    """
    Start an Apify actor run for a given platform + keyword.
    Returns the job row dict (including the Apify run_id).
    Persists an apify_jobs row immediately so we can poll it later.
    """
    with get_db() as conn:
        # Allow actor override from integrations config
        row = conn.execute(
            "SELECT config FROM integrations WHERE service='apify'"
        ).fetchone()
        cfg = json.loads(row["config"]) if row and row["config"] else {}

    actor_map = {**APIFY_ACTOR_MAP, **cfg.get("actor_map", {})}
    actor = actor_id or actor_map.get(platform.lower())
    if not actor:
        raise ValueError(f"No Apify actor configured for platform '{platform}'")

    run_input = _default_input(platform, keyword)

    url = f"{_API_BASE}/acts/{actor}/runs"
    r = requests.post(url, headers=_headers(), json=run_input, timeout=30)
    r.raise_for_status()
    run_data = r.json().get("data", {})
    run_id   = run_data.get("id")

    job_id = str(uuid.uuid4())
    with get_db() as conn:
        conn.execute("""
            INSERT INTO apify_jobs (id, run_id, actor, keyword, platform, status)
            VALUES (?, ?, ?, ?, ?, 'running')
        """, (job_id, run_id, actor, keyword, platform))
        conn.commit()

    print(f"[Apify] Started {actor} run {run_id} for '{keyword}' on {platform}")
    return {"job_id": job_id, "run_id": run_id, "actor": actor}


def get_run_status(run_id: str) -> str:
    """Return Apify run status string: RUNNING, SUCCEEDED, FAILED, ABORTED, TIMED-OUT"""
    url = f"{_API_BASE}/actor-runs/{run_id}"
    r = requests.get(url, headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json().get("data", {}).get("status", "UNKNOWN")


def get_run_results(run_id: str) -> list:
    """Fetch dataset items from a completed Apify run. Returns list of raw item dicts."""
    url = f"{_API_BASE}/actor-runs/{run_id}/dataset/items"
    params = {"format": "json", "limit": 200}
    r = requests.get(url, headers=_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


# ── Normalization ─────────────────────────────────────────────────────────────

def normalize_tiktok(item: dict, keyword: str) -> dict | None:
    text  = item.get("text") or item.get("description") or ""
    title = (text[:100] + "…" if len(text) > 100 else text) or item.get("id", "TikTok video")
    if not title.strip():
        return None
    return {
        "url":              item.get("webVideoUrl") or item.get("url"),
        "title":            title,
        "snippet":          text[:500],
        "author":           item.get("authorMeta", {}).get("name") or item.get("author"),
        "source_name":      "tiktok",
        "platform":         "TikTok",
        "published_at":     _parse_ts(item.get("createTime") or item.get("createTimeISO")),
        "engagement_count": item.get("diggCount") or item.get("stats", {}).get("diggCount"),
        "engagement_label": "likes",
        "matched_keyword":  keyword,
        "connector":        "apify",
    }


def normalize_instagram(item: dict, keyword: str) -> dict | None:
    caption = item.get("caption") or item.get("text") or ""
    title   = (caption[:100] + "…" if len(caption) > 100 else caption) or "Instagram post"
    return {
        "url":              item.get("url") or item.get("shortCode") and f"https://instagram.com/p/{item['shortCode']}/",
        "title":            title,
        "snippet":          caption[:500],
        "author":           item.get("ownerUsername") or item.get("owner", {}).get("username"),
        "source_name":      "instagram",
        "platform":         "Instagram",
        "published_at":     _parse_ts(item.get("timestamp") or item.get("takenAtTimestamp")),
        "engagement_count": item.get("likesCount") or item.get("likes"),
        "engagement_label": "likes",
        "matched_keyword":  keyword,
        "connector":        "apify",
    }


def normalize_reddit(item: dict, keyword: str) -> dict | None:
    title = item.get("title") or ""
    body  = item.get("selftext") or item.get("body") or ""
    if not title:
        return None
    return {
        "url":              item.get("url") or (item.get("permalink") and f"https://reddit.com{item['permalink']}"),
        "title":            title[:200],
        "snippet":          body[:500],
        "author":           item.get("author"),
        "source_name":      "reddit",
        "platform":         f"Reddit r/{item.get('subreddit', 'unknown')}",
        "published_at":     _parse_ts(item.get("created") or item.get("createdAt")),
        "engagement_count": item.get("score") or item.get("ups"),
        "engagement_label": "upvotes",
        "matched_keyword":  keyword,
        "connector":        "apify",
    }


def normalize_youtube(item: dict, keyword: str) -> dict | None:
    title = item.get("title") or ""
    desc  = item.get("description") or ""
    if not title:
        return None
    vid_id = item.get("id") or item.get("videoId") or ""
    url    = f"https://youtube.com/watch?v={vid_id}" if vid_id else item.get("url")
    return {
        "url":              url,
        "title":            title[:200],
        "snippet":          desc[:400],
        "author":           item.get("channelName") or item.get("channel", {}).get("name"),
        "source_name":      "youtube",
        "platform":         "YouTube",
        "published_at":     _parse_ts(item.get("date") or item.get("publishedAt")),
        "engagement_count": item.get("viewCount") or item.get("views"),
        "engagement_label": "views",
        "matched_keyword":  keyword,
        "connector":        "apify",
    }


def normalize_twitter(item: dict, keyword: str) -> dict | None:
    text = item.get("full_text") or item.get("text") or ""
    if not text:
        return None
    return {
        "url":              item.get("url") or (item.get("id_str") and f"https://x.com/i/web/status/{item['id_str']}"),
        "title":            (text[:100] + "…" if len(text) > 100 else text),
        "snippet":          text[:500],
        "author":           item.get("user", {}).get("screen_name") or item.get("author_id"),
        "source_name":      "x",
        "platform":         "X / Twitter",
        "published_at":     _parse_ts(item.get("created_at")),
        "engagement_count": item.get("favorite_count") or item.get("public_metrics", {}).get("like_count"),
        "engagement_label": "likes",
        "matched_keyword":  keyword,
        "connector":        "apify",
    }


_NORMALIZERS = {
    "tiktok":    normalize_tiktok,
    "instagram": normalize_instagram,
    "reddit":    normalize_reddit,
    "youtube":   normalize_youtube,
    "twitter":   normalize_twitter,
    "x":         normalize_twitter,
}


def normalize_result(item: dict, platform: str, keyword: str) -> dict | None:
    """Dispatch to the right normalizer for this platform."""
    fn = _NORMALIZERS.get(platform.lower())
    if not fn:
        return None
    return fn(item, keyword)


def _parse_ts(val) -> str | None:
    if not val:
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.utcfromtimestamp(val).isoformat()
        except Exception:
            return None
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00")).isoformat()
        except Exception:
            return None
    return None


# ── Full job cycle ─────────────────────────────────────────────────────────────

def poll_and_ingest_job(job_id: str) -> dict:
    """
    Poll an Apify job until complete, then ingest results into mentions.
    Designed to run in a background thread.
    Returns {status, new_count, error}.
    """
    from ai_analysis import analyze_mention
    from ranker import calculate_score
    from scanner import _is_brand_relevant

    with get_db() as conn:
        job = dict(conn.execute("SELECT * FROM apify_jobs WHERE id=?", (job_id,)).fetchone() or {})

    if not job:
        return {"status": "not_found", "new_count": 0}

    run_id   = job["run_id"]
    platform = job["platform"]
    keyword  = job["keyword"]

    # Poll with back-off (max 10 min)
    for attempt in range(40):
        status = get_run_status(run_id)
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            with get_db() as conn:
                conn.execute("UPDATE apify_jobs SET status=? WHERE id=?", (status.lower(), job_id))
                conn.commit()
            return {"status": status.lower(), "new_count": 0}
        time.sleep(15)
    else:
        return {"status": "timeout", "new_count": 0}

    items = get_run_results(run_id)
    new_count = 0

    with get_db() as conn:
        existing_urls   = {row[0] for row in conn.execute("SELECT url FROM mentions WHERE url IS NOT NULL").fetchall()}
        existing_titles = {row[0][:60].lower() for row in conn.execute("SELECT title FROM mentions").fetchall()}

        for item in items:
            raw = normalize_result(item, platform, keyword)
            if not raw or not raw.get("title"):
                continue

            # Brand relevance filter
            if not _is_brand_relevant(raw["title"], raw.get("snippet", ""), keyword):
                continue

            # Dedup
            norm_url   = raw.get("url", "").rstrip("/").split("?")[0] if raw.get("url") else ""
            norm_title = raw["title"].lower()[:60]
            if (norm_url and norm_url in existing_urls) or norm_title in existing_titles:
                continue

            ai    = analyze_mention(raw["title"], raw.get("snippet", ""))
            score = calculate_score(
                source_name=raw["source_name"],
                sentiment=ai["sentiment"],
                engagement_count=raw.get("engagement_count"),
                published_at=raw.get("published_at"),
                title=raw["title"],
                snippet=raw.get("snippet", ""),
                narrative_type=ai.get("narrative_type"),
            )

            mid = str(uuid.uuid4())
            conn.execute("""
                INSERT OR IGNORE INTO mentions
                  (id, url, title, snippet, author, source_name, platform,
                   published_at, engagement_count, engagement_label,
                   connector, ai_used,
                   sentiment, risk_level, impact_score,
                   narrative_type, patient_outreach_needed, response_draft,
                   ai_summary, why_it_matters, recommended_action,
                   notify_leadership, needs_legal_review, public_response,
                   is_opportunity, is_threat, raw_score_factors, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                mid, raw.get("url"), raw["title"], raw.get("snippet", ""),
                raw.get("author"), raw["source_name"], raw["platform"],
                raw.get("published_at"),
                raw.get("engagement_count"), raw.get("engagement_label"),
                "apify", 1,
                ai["sentiment"], score["risk_level"], score["impact_score"],
                ai.get("narrative_type", "general_mention"),
                int(ai.get("patient_outreach_needed", False)),
                ai.get("response_draft", ""),
                ai["ai_summary"], ai["why_it_matters"], ai["recommended_action"],
                int(score["notify_leadership"]), int(score["needs_legal_review"]),
                int(ai.get("public_response", False)),
                int(score["is_opportunity"]), int(score["is_threat"]),
                json.dumps(score["score_factors"]), "new",
            ))

            if norm_url:   existing_urls.add(norm_url)
            if norm_title: existing_titles.add(norm_title)
            new_count += 1

        conn.execute("""
            UPDATE apify_jobs SET status='completed', items_count=?, completed_at=datetime('now')
            WHERE id=?
        """, (new_count, job_id))
        conn.commit()

    print(f"[Apify] Job {job_id}: {new_count} new mentions ingested")
    return {"status": "completed", "new_count": new_count}
