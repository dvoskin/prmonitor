"""
Scanner — orchestrates all connectors, deduplicates by URL + title similarity,
runs AI analysis, scores each mention, and persists to DB.

Active connectors (all free, no API key required):
  - Google News RSS     → news articles
  - DuckDuckGo Web      → broad web (blogs, review sites, BBB, Yelp, etc.)
  - Reddit              → forum discussion (brand-filtered)
  - YouTube             → video content

Optional (add API key to .env):
  - Serper.dev          → Google Search + News (higher quality/volume)
"""

import uuid
import json
import time
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from db import get_db
from ranker import calculate_score
from ai_analysis import analyze_mention

# Always-on connectors (zero API keys needed)
from connectors.google_news_rss import search_google_news_rss
from connectors.bing_news import search_bing_news
from connectors.reddit import search_reddit
from connectors.youtube import search_youtube
from connectors.review_sites import search_review_sites
from connectors.podcasts import search_podcasts

# Optional paid connectors (only used if key is set)
from connectors.serper import search_google, search_google_news

# Brand relevance filter — results missing all of these are dropped from DDG/YT
BRAND_TERMS = [
    "goals plastic surgery", "goals aesthetics", "goals surgery",
    "goalsplasticsurgery", "dr. voskin", "dr voskin", "sergey voskin",
    "flexsculpt", "doublebbl", "goals bbl", "goals lipo", "goals ps",
]

# How many keywords to query per connector (avoid hammering free endpoints)
MAX_KW_PER_CONNECTOR = {
    "google_news_rss": 18,   # very fast RSS
    "duckduckgo":      8,    # HTML scraping — be polite
    "reddit":          6,    # only run top/most specific keywords (speed)
    "youtube":         5,
    "review_sites":    8,    # Google News RSS site: operator — fast
    "podcasts":        6,    # iTunes API + Google News podcast directories
}

DELAY_BETWEEN_REQUESTS = 0.8  # seconds — be a good citizen

# Parallel scan config
# Each (connector, keyword) pair becomes one task.
# MAX_WORKERS controls how many run simultaneously.
# Free RSS/scraping endpoints: 12 concurrent is safe.
# If you hit rate limits, lower this.
MAX_WORKERS = 12

# Per-connector concurrency cap — don't hammer one host too hard
MAX_CONCURRENT_PER_HOST = {
    "google_news_rss": 6,
    "bing_news":       4,
    "reddit":          3,
    "youtube":         3,
    "review_sites":    4,
    "podcasts":        3,
}

# Semaphores enforce per-host caps at runtime
_semaphores: dict = {}

def _get_sem(connector: str) -> threading.Semaphore:
    if connector not in _semaphores:
        _semaphores[connector] = threading.Semaphore(MAX_CONCURRENT_PER_HOST.get(connector, 4))
    return _semaphores[connector]


def _run_connector(connector_fn, connector_name: str, keyword: str) -> list:
    """Call one connector for one keyword, with per-host concurrency limit."""
    sem = _get_sem(connector_name)
    with sem:
        try:
            results = connector_fn(keyword)
            return results or []
        except Exception as e:
            print(f"[Scanner] {connector_name} error for {keyword!r}: {e}")
            return []


def _is_brand_relevant(title: str, snippet: str, matched_keyword: str = "") -> bool:
    """
    Returns True if the content is specifically about Goals PS or related entities.

    Key insight: RSS snippets (Google News, Bing) are often just the publication name
    ("NBC News"), NOT the article body. If the search query itself was a brand term,
    trust the result — Google/Bing only return it because the article body matched.
    """
    # If the matched keyword is itself a brand term, trust the search engine's result
    kw = matched_keyword.lower()
    if any(term in kw for term in BRAND_TERMS):
        return True
    # Otherwise fall back to text scanning of title + snippet
    text = (title + " " + (snippet or "")).lower()
    return any(term in text for term in BRAND_TERMS)


def _normalize_url(url: str) -> str:
    """Remove query params / anchors for dedup."""
    return re.sub(r"[?#].*", "", url.rstrip("/"))


def _title_key(title: str) -> str:
    """Fuzzy dedup key — lowercase, strip punctuation, first 60 chars."""
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:60].strip()


def run_scan() -> dict:
    start_ms = int(time.time() * 1000)
    scan_id  = str(uuid.uuid4())

    with get_db() as conn:
        conn.execute("INSERT INTO scan_logs (id, status) VALUES (?, 'running')", (scan_id,))
        conn.commit()

    try:
        # Load enabled keywords
        with get_db() as conn:
            keywords = [dict(r) for r in conn.execute(
                "SELECT id, phrase FROM keywords WHERE enabled = 1"
            ).fetchall()]

        import os
        has_serper = bool(os.getenv("SERPER_API_KEY"))

        all_raw = []

        # ── Build all tasks: (connector_fn, connector_name, keyword_phrase) ─
        # Each task is one HTTP fetch. They all run in parallel up to MAX_WORKERS.
        tasks = []

        connector_map = [
            (search_google_news_rss, "google_news_rss", MAX_KW_PER_CONNECTOR["google_news_rss"]),
            (search_bing_news,       "bing_news",        MAX_KW_PER_CONNECTOR["duckduckgo"]),
            (search_reddit,          "reddit",            MAX_KW_PER_CONNECTOR["reddit"]),
            (search_youtube,         "youtube",           MAX_KW_PER_CONNECTOR["youtube"]),
            (search_review_sites,    "review_sites",      MAX_KW_PER_CONNECTOR["review_sites"]),
            (search_podcasts,        "podcasts",           MAX_KW_PER_CONNECTOR["podcasts"]),
        ]

        if has_serper:
            connector_map += [
                (search_google,      "serper_web",  len(keywords)),
                (search_google_news, "serper_news", len(keywords)),
            ]

        for fn, name, limit in connector_map:
            for kw in keywords[:limit]:
                tasks.append((fn, name, kw["phrase"]))

        total_tasks = len(tasks)
        print(f"[Scanner] Dispatching {total_tasks} tasks across {MAX_WORKERS} workers…")
        t0 = time.time()

        completed = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_run_connector, fn, name, phrase): (name, phrase)
                for fn, name, phrase in tasks
            }
            for future in as_completed(futures):
                name, phrase = futures[future]
                results = future.result()
                all_raw.extend(results)
                completed += 1
                if completed % 10 == 0 or completed == total_tasks:
                    print(f"[Scanner]   {completed}/{total_tasks} done — {len(all_raw)} raw results so far")

        print(f"[Scanner] All tasks done in {time.time()-t0:.1f}s")

        # ── Brand relevance gate — pass matched_keyword so RSS snippets aren't penalized ──
        before_filter = len(all_raw)
        all_raw = [r for r in all_raw if _is_brand_relevant(
            r.get("title", ""), r.get("snippet", ""), r.get("matched_keyword", "")
        )]
        print(f"[Scanner] Raw total: {before_filter} → {len(all_raw)} after brand filter")

        # ── Deduplicate ─────────────────────────────────────────────────────
        seen_urls   = set()
        seen_titles = set()
        deduped     = []

        with get_db() as conn:
            existing_urls = {
                row[0] for row in conn.execute(
                    "SELECT url FROM mentions WHERE url IS NOT NULL"
                ).fetchall()
            }
            existing_titles = {
                _title_key(row[0]) for row in conn.execute(
                    "SELECT title FROM mentions"
                ).fetchall()
            }

        for r in all_raw:
            url   = r.get("url", "")
            title = r.get("title", "")
            norm_url   = _normalize_url(url) if url else ""
            norm_title = _title_key(title)

            if norm_url and (norm_url in existing_urls or norm_url in seen_urls):
                continue
            if norm_title and (norm_title in existing_titles or norm_title in seen_titles):
                continue
            if not title:
                continue

            if norm_url: seen_urls.add(norm_url)
            if norm_title: seen_titles.add(norm_title)
            deduped.append(r)

        print(f"[Scanner] After dedup: {len(deduped)} new results to process")

        # ── Analyze, score, persist ─────────────────────────────────────────
        new_count = 0

        with get_db() as conn:
            kw_map = {
                row["phrase"]: row["id"]
                for row in conn.execute("SELECT id, phrase FROM keywords").fetchall()
            }

            for raw in deduped:
                title   = raw.get("title", "Untitled")
                snippet = raw.get("snippet", "")

                ai    = analyze_mention(title, snippet)
                score = calculate_score(
                    source_name=raw["source_name"],
                    sentiment=ai["sentiment"],
                    engagement_count=raw.get("engagement_count"),
                    published_at=raw.get("published_at"),
                    title=title,
                    snippet=snippet,
                    related_surgeon=raw.get("related_surgeon"),
                    related_location=raw.get("related_location"),
                    related_procedure=raw.get("related_procedure"),
                )

                mid = str(uuid.uuid4())
                url = raw.get("url") or None

                conn.execute("""
                    INSERT OR IGNORE INTO mentions
                      (id, url, title, snippet, author, source_name, platform,
                       published_at, engagement_count, engagement_label,
                       sentiment, risk_level, impact_score,
                       ai_summary, why_it_matters, recommended_action,
                       notify_leadership, needs_legal_review, public_response,
                       is_opportunity, is_threat, raw_score_factors, status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    mid, url, title, snippet,
                    raw.get("author"), raw["source_name"], raw.get("platform"),
                    raw.get("published_at"),
                    raw.get("engagement_count"), raw.get("engagement_label"),
                    ai["sentiment"], score["risk_level"], score["impact_score"],
                    ai["ai_summary"], ai["why_it_matters"], ai["recommended_action"],
                    int(score["notify_leadership"]), int(score["needs_legal_review"]),
                    int(ai.get("public_response", False)),
                    int(score["is_opportunity"]), int(score["is_threat"]),
                    json.dumps(score["score_factors"]), "new",
                ))

                # Link keywords
                kw_phrase = raw.get("matched_keyword", "")
                kw_id = kw_map.get(kw_phrase)
                if kw_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO mention_keywords VALUES (?,?)", (mid, kw_id)
                    )

                # Alert for high/critical
                level = score["risk_level"]
                if level in ("critical", "high"):
                    conn.execute("""
                        INSERT INTO alerts (id, mention_id, type, title, body, severity)
                        VALUES (?,?,?,?,?,?)
                    """, (
                        str(uuid.uuid4()), mid,
                        "critical_mention" if level == "critical" else "high_risk",
                        f"{'Critical' if level == 'critical' else 'High Risk'}: {title[:70]}",
                        ai["ai_summary"], level,
                    ))

                new_count += 1

            duration = int(time.time() * 1000) - start_ms
            sources  = ["google_news_rss", "bing_news_rss", "reddit", "youtube", "review_sites", "podcasts"]
            if has_serper:
                sources += ["serper_google", "serper_news"]

            conn.execute("""
                UPDATE scan_logs SET status='completed', new_mentions_count=?,
                total_scanned=?, sources_scanned=?, duration_ms=? WHERE id=?
            """, (new_count, len(deduped), json.dumps(sources), duration, scan_id))
            conn.commit()

        print(f"[Scanner] Done — {new_count} new mentions saved in {duration}ms")
        return {"new_count": new_count, "scanned": len(deduped)}

    except Exception as e:
        with get_db() as conn:
            conn.execute(
                "UPDATE scan_logs SET status='failed', error_message=? WHERE id=?",
                (str(e), scan_id)
            )
            conn.commit()
        raise
