"""
Google Business Profile connector — syncs reviews for all Goals locations.

Setup (one-time, per environment):
  1. Create a project in Google Cloud Console
  2. Enable the "My Business Account Management API" and "My Business Business Information API"
  3. Create OAuth 2.0 credentials (Web application type)
  4. Set Authorized redirect URI to: {APP_URL}/api/integrations/google/callback
  5. Add to .env:
       GOOGLE_CLIENT_ID=...
       GOOGLE_CLIENT_SECRET=...
       GOOGLE_REDIRECT_URI=https://your-app.onrender.com/api/integrations/google/callback

OAuth scopes used:
  - https://www.googleapis.com/auth/business.manage
"""

import os
import json
import uuid
import time
import urllib.parse
from datetime import datetime, timezone

import requests

from db import get_db

# ── OAuth endpoints ──────────────────────────────────────────────────────────
_AUTH_BASE    = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL    = "https://oauth2.googleapis.com/token"
_SCOPE        = "https://www.googleapis.com/auth/business.manage"

# ── GBP API base URLs ────────────────────────────────────────────────────────
_ACCT_URL     = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
_REVIEW_BASE  = "https://mybusiness.googleapis.com/v4"       # legacy v4 (still active for reviews)
_BUSINESS_URL = "https://mybusinessbusinessinformation.googleapis.com/v1"


def get_oauth_url() -> str:
    """Return the Google consent-screen URL the user should visit."""
    client_id    = os.getenv("GOOGLE_CLIENT_ID", "")
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "")
    params = {
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         _SCOPE,
        "access_type":   "offline",
        "prompt":        "consent",          # force refresh_token every time
    }
    return f"{_AUTH_BASE}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Exchange OAuth authorization code for access + refresh tokens."""
    r = requests.post(_TOKEN_URL, data={
        "code":          code,
        "client_id":     os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "redirect_uri":  os.getenv("GOOGLE_REDIRECT_URI", ""),
        "grant_type":    "authorization_code",
    }, timeout=15)
    r.raise_for_status()
    return r.json()   # {access_token, refresh_token, expires_in, token_type}


def refresh_access_token(refresh_token: str) -> dict:
    """Use refresh_token to get a new access_token."""
    r = requests.post(_TOKEN_URL, data={
        "refresh_token": refresh_token,
        "client_id":     os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "grant_type":    "refresh_token",
    }, timeout=15)
    r.raise_for_status()
    return r.json()   # {access_token, expires_in}


def _get_valid_token() -> str:
    """
    Load stored token from DB.  Refresh if expired.  Return access_token string.
    Raises ValueError if Google integration is not connected.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM integrations WHERE service='google_business'"
        ).fetchone()

    if not row or not row["connected"]:
        raise ValueError("Google Business Profile not connected. Visit /api/integrations/google/auth first.")

    expiry = row["token_expiry"]
    if expiry:
        exp_dt = datetime.fromisoformat(expiry)
        if datetime.now(timezone.utc) >= exp_dt:
            # Refresh
            new_tokens = refresh_access_token(row["refresh_token"])
            new_expiry = datetime.now(timezone.utc).replace(microsecond=0)
            new_expiry = new_expiry.isoformat()
            with get_db() as conn:
                conn.execute("""
                    UPDATE integrations
                    SET access_token=?, token_expiry=?, updated_at=datetime('now')
                    WHERE service='google_business'
                """, (new_tokens["access_token"], new_expiry))
                conn.commit()
            return new_tokens["access_token"]

    return row["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Account / location discovery ─────────────────────────────────────────────

def list_accounts() -> list:
    """Return all GBP accounts accessible under the connected credential."""
    token = _get_valid_token()
    r = requests.get(_ACCT_URL, headers=_auth_headers(token), timeout=15)
    r.raise_for_status()
    return r.json().get("accounts", [])


def list_locations(account_name: str) -> list:
    """Return all locations for a given account (e.g. 'accounts/123456')."""
    token = _get_valid_token()
    url = f"{_ACCT_URL}/{account_name.split('/')[-1]}/locations"
    params = {"readMask": "name,title,storefrontAddress"}
    r = requests.get(url, headers=_auth_headers(token), params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("locations", [])


# ── Review sync ──────────────────────────────────────────────────────────────

def _fetch_reviews(token: str, location_name: str) -> list:
    """Paginate through all reviews for a location. Returns list of raw review dicts."""
    reviews = []
    page_token = None
    url = f"{_REVIEW_BASE}/{location_name}/reviews"

    while True:
        params = {"pageSize": 50}
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(url, headers=_auth_headers(token), params=params, timeout=20)
        if r.status_code == 404:
            print(f"[GBP] Location {location_name} returned 404 — skipping")
            break
        r.raise_for_status()
        data = r.json()
        reviews.extend(data.get("reviews", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return reviews


def _normalize_review(review: dict, location_title: str, location_name: str) -> dict:
    """Convert a raw GBP review dict to our mention schema."""
    # Rating → star_rating
    rating_map = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
    stars = rating_map.get(review.get("starRating", ""), None)

    # Sentiment from rating
    if stars is not None:
        if stars <= 2:
            sentiment = "negative"
        elif stars == 3:
            sentiment = "neutral"
        else:
            sentiment = "positive"
    else:
        sentiment = "neutral"

    # Author
    author_info = review.get("reviewer", {})
    author = author_info.get("displayName", "Anonymous")

    # Comment text
    comment = review.get("comment", "").strip()

    # Review reply status
    reply = review.get("reviewReply")
    reply_status = "replied" if reply else "pending"

    # Timestamps
    create_time = review.get("createTime", "")
    try:
        pub_dt = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
        published_at = pub_dt.isoformat()
    except Exception:
        published_at = None

    # URL — GBP doesn't expose a direct review URL; build a search-based fallback
    review_name = review.get("name", "")

    title = f"Google Review — {location_title} ({stars}★ by {author})"
    snippet = comment[:400] if comment else "No review text provided."

    return {
        "google_review_id": review_name,
        "url":              None,          # GBP reviews don't have public URLs via API
        "title":            title,
        "snippet":          snippet,
        "author":           author,
        "source_name":      "google_reviews",
        "platform":         f"Google Reviews — {location_title}",
        "published_at":     published_at,
        "engagement_count": None,
        "engagement_label": None,
        "related_location": location_title,
        "star_rating":      stars,
        "reply_status":     reply_status,
        "reply_available":  1,
        "sentiment":        sentiment,
        "matched_keyword":  "google_reviews",
        "connector":        "google_business_profile",
    }


def sync_location_reviews(location_name: str, location_title: str) -> int:
    """
    Fetch all reviews for one location, normalize, and upsert into mentions.
    Returns count of new mentions saved.
    """
    from ai_analysis import analyze_mention
    from ranker import calculate_score

    token = _get_valid_token()
    raw_reviews = _fetch_reviews(token, location_name)
    print(f"[GBP] {location_title}: fetched {len(raw_reviews)} reviews")

    new_count = 0
    with get_db() as conn:
        for review in raw_reviews:
            norm = _normalize_review(review, location_title, location_name)
            g_id = norm["google_review_id"]

            # Dedup: skip if this google_review_id already exists
            existing = conn.execute(
                "SELECT id FROM mentions WHERE google_review_id=?", (g_id,)
            ).fetchone()
            if existing:
                continue

            # AI analysis only for 1-2 star reviews (high-priority)
            stars = norm["star_rating"]
            if stars is not None and stars <= 2:
                ai = analyze_mention(norm["title"], norm["snippet"])
            else:
                # Rule-based fallback for 3-5 star
                ai = {
                    "sentiment":              norm["sentiment"],
                    "narrative_type":         "positive_review" if stars and stars >= 4 else "general_mention",
                    "ai_summary":             norm["snippet"][:200],
                    "why_it_matters":         f"{stars}★ Google review at {location_title}",
                    "recommended_action":     "Monitor" if stars and stars >= 3 else "Respond promptly",
                    "patient_outreach_needed":False,
                    "public_response":        stars is not None and stars <= 3,
                    "response_draft":         "",
                }

            score = calculate_score(
                source_name=norm["source_name"],
                sentiment=ai["sentiment"],
                published_at=norm.get("published_at"),
                title=norm["title"],
                snippet=norm["snippet"],
                related_location=norm.get("related_location"),
                narrative_type=ai.get("narrative_type"),
            )

            mid = str(uuid.uuid4())
            conn.execute("""
                INSERT OR IGNORE INTO mentions
                  (id, url, title, snippet, author, source_name, platform,
                   published_at, engagement_count, engagement_label,
                   related_location, star_rating, reply_status, reply_available,
                   google_review_id, connector, ai_used,
                   sentiment, risk_level, impact_score,
                   narrative_type, patient_outreach_needed, response_draft,
                   ai_summary, why_it_matters, recommended_action,
                   notify_leadership, needs_legal_review, public_response,
                   is_opportunity, is_threat, raw_score_factors, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                mid, norm["url"], norm["title"], norm["snippet"],
                norm["author"], norm["source_name"], norm["platform"],
                norm["published_at"], norm["engagement_count"], norm["engagement_label"],
                norm["related_location"], norm["star_rating"], norm["reply_status"], norm["reply_available"],
                norm["google_review_id"], norm["connector"], int(stars is not None and stars <= 2),
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

            # Alert for 1-2 star
            if stars is not None and stars <= 2:
                lvl = score["risk_level"]
                conn.execute("""
                    INSERT INTO alerts (id, mention_id, type, title, body, severity)
                    VALUES (?,?,?,?,?,?)
                """, (
                    str(uuid.uuid4()), mid,
                    "google_low_rating",
                    f"{stars}★ Google Review — {location_title}: {norm['author']}",
                    norm["snippet"][:120],
                    lvl,
                ))

            new_count += 1

        # Update last_synced for this location
        conn.execute("""
            UPDATE google_locations SET last_synced=datetime('now')
            WHERE name=?
        """, (location_name,))
        conn.commit()

    return new_count


def sync_all_locations() -> dict:
    """Sync reviews for every stored location. Returns {location: new_count}."""
    with get_db() as conn:
        locations = [dict(r) for r in conn.execute(
            "SELECT name, title FROM google_locations"
        ).fetchall()]

    results = {}
    for loc in locations:
        try:
            n = sync_location_reviews(loc["name"], loc["title"] or loc["name"])
            results[loc["title"] or loc["name"]] = n
        except Exception as e:
            results[loc["title"] or loc["name"]] = f"error: {e}"

    return results


def reply_to_review(review_name: str, reply_text: str) -> bool:
    """
    Post or update a reply to a Google review.
    review_name format: 'accounts/{account}/locations/{location}/reviews/{review}'
    Returns True on success.
    """
    token = _get_valid_token()
    url = f"{_REVIEW_BASE}/{review_name}/reply"
    r = requests.put(url, headers=_auth_headers(token),
                     json={"comment": reply_text}, timeout=15)
    if r.ok:
        # Update reply_status in DB
        with get_db() as conn:
            conn.execute(
                "UPDATE mentions SET reply_status='replied', updated_at=datetime('now') WHERE google_review_id=?",
                (review_name,)
            )
            conn.commit()
        return True
    print(f"[GBP] Reply failed: {r.status_code} {r.text[:200]}")
    return False


def store_tokens(access_token: str, refresh_token: str, expires_in: int) -> None:
    """Persist OAuth tokens to the integrations table."""
    from datetime import timezone, timedelta
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO integrations (id, service, access_token, refresh_token, token_expiry, connected)
            VALUES (?, 'google_business', ?, ?, ?, 1)
            ON CONFLICT(service) DO UPDATE SET
              access_token=excluded.access_token,
              refresh_token=COALESCE(excluded.refresh_token, refresh_token),
              token_expiry=excluded.token_expiry,
              connected=1,
              updated_at=datetime('now')
        """, (str(uuid.uuid4()), access_token, refresh_token, expiry))
        conn.commit()
