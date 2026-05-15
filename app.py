"""
Goals Plastic Surgery — PR Impact Monitor
Flask backend + SQLite. Run: python3 app.py
"""

import os
import uuid
import json
import threading
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, jsonify, redirect, url_for
from db import init_db, seed_db, get_db
from ranker import calculate_score, risk_color, sentiment_color, NARRATIVE_LABELS
from ai_analysis import analyze_mention
from scanner import run_scan

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "goals-pr-monitor-dev-key")

# ── Initialize DB at import time so gunicorn picks it up ─────────────────────
init_db()
with get_db() as _c:
    _count = _c.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
if _count == 0:
    seed_db()
    print("✅ Fresh database seeded")
else:
    print(f"✅ Database ready — {_count} mentions loaded")

# ── Background scan state ────────────────────────────────────────────────────
_scan_lock  = threading.Lock()
_scan_state = {"running": False, "new_count": 0, "error": None}


# ── Helpers ──────────────────────────────────────────────────────────────────

def row_to_dict(row):
    return dict(row) if row else None

def rows_to_list(rows):
    return [dict(r) for r in rows]

def get_stats():
    with get_db() as conn:
        total     = conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
        new_count = conn.execute("SELECT COUNT(*) FROM mentions WHERE status='new'").fetchone()[0]
        negative  = conn.execute("SELECT COUNT(*) FROM mentions WHERE sentiment='negative'").fetchone()[0]
        critical  = conn.execute("SELECT COUNT(*) FROM mentions WHERE risk_level='critical'").fetchone()[0]
        positive  = conn.execute("SELECT COUNT(*) FROM mentions WHERE sentiment='positive'").fetchone()[0]
        opps      = conn.execute("SELECT COUNT(*) FROM mentions WHERE is_opportunity=1").fetchone()[0]
        legal     = conn.execute("SELECT COUNT(*) FROM mentions WHERE needs_legal_review=1 AND status != 'resolved'").fetchone()[0]
        archived  = conn.execute("SELECT COUNT(*) FROM mentions WHERE status='archived'").fetchone()[0]
        unread    = conn.execute("SELECT COUNT(*) FROM alerts WHERE read=0").fetchone()[0]
        last_scan = conn.execute("SELECT scanned_at, status FROM scan_logs ORDER BY scanned_at DESC LIMIT 1").fetchone()
        public_response = conn.execute(
            "SELECT COUNT(*) FROM mentions WHERE public_response=1 AND status NOT IN ('resolved','archived')"
        ).fetchone()[0]
        outreach = conn.execute(
            "SELECT COUNT(*) FROM mentions WHERE patient_outreach_needed=1 AND status NOT IN ('resolved','archived')"
        ).fetchone()[0]
    return {
        "total": total, "new": new_count, "negative": negative,
        "positive": positive, "critical": critical,
        "opportunities": opps, "legal_queue": legal, "archived": archived,
        "unread_alerts": unread,
        "last_scan": last_scan["scanned_at"] if last_scan else None,
        "last_scan_status": last_scan["status"] if last_scan else None,
        "public_response": public_response,
        "outreach": outreach,
    }


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/radar")
def radar_page():
    stats = get_stats()
    return render_template("radar.html", stats=stats)


@app.route("/api/radar")
def api_radar():
    """
    Cluster active mentions into narrative/incident nodes for the Reputation Radar.
    Groups by (narrative_type × location/procedure), computes velocity and severity.
    """
    from collections import defaultdict

    SEVERITY_RANK = {"critical": 4, "high": 3, "moderate": 2, "low": 1}
    RANK_SEVERITY = {4: "critical", 3: "high", 2: "moderate", 1: "low"}

    NARRATIVE_TITLE = {
        "botched_allegation":    "Botched Allegation",
        "safety_complaint":      "Patient Safety Concern",
        "legal_threat":          "Legal Action",
        "payment_dispute":       "Payment Dispute",
        "staff_complaint":       "Staff Complaint",
        "scheduling_complaint":  "Scheduling Issues",
        "recovery_concern":      "Recovery Concern",
        "communication_failure": "No Follow-Up Reports",
        "positive_review":       "Positive Mentions",
        "general_mention":       "General Mentions",
    }

    def _days_ago(ts):
        if not ts:
            return 999
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dt = dt.replace(tzinfo=None) if dt.tzinfo else dt
            return (datetime.utcnow() - dt).days
        except Exception:
            return 999

    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT id, title, snippet, platform, source_name, sentiment, risk_level,
                   impact_score, narrative_type, discovered_at, published_at,
                   engagement_count, related_location, related_procedure,
                   ai_summary, why_it_matters, recommended_action,
                   patient_outreach_needed, needs_legal_review, status
            FROM mentions
            WHERE status NOT IN ('archived')
              AND discovered_at >= datetime('now', '-60 days')
        """).fetchall()]

    # Cluster by narrative_type + location (or procedure if no location)
    clusters = defaultdict(list)
    for r in rows:
        nt  = r.get("narrative_type") or "general_mention"
        loc = (r.get("related_location") or "").strip()
        proc = (r.get("related_procedure") or "").strip()
        key = f"{nt}||{loc}" if loc else (f"{nt}||{proc}" if proc else nt)
        clusters[key].append(r)

    nodes = []
    for key, mentions in clusters.items():
        if not mentions:
            continue
        mentions.sort(key=lambda m: m.get("impact_score") or 0, reverse=True)
        top = mentions[0]

        # Velocity: mentions last 7d vs prior 7d
        last_7d  = sum(1 for m in mentions if _days_ago(m.get("discovered_at")) <= 7)
        last_14d = sum(1 for m in mentions if _days_ago(m.get("discovered_at")) <= 14)
        prior_7d = last_14d - last_7d
        velocity = round(last_7d / max(prior_7d, 0.5), 2)
        if last_7d == 0:
            trend = "declining"
        elif velocity >= 1.4:
            trend = "rising"
        else:
            trend = "stable"

        # Max severity in cluster
        max_rank = max((SEVERITY_RANK.get(m.get("risk_level", "low"), 1) for m in mentions), default=1)
        severity = RANK_SEVERITY.get(max_rank, "low")

        # Title
        nt   = top.get("narrative_type") or "general_mention"
        base = NARRATIVE_TITLE.get(nt, nt.replace("_", " ").title())
        loc  = top.get("related_location", "") or ""
        proc = top.get("related_procedure", "") or ""
        title = f"{base} — {loc}" if loc else (f"{base} — {proc}" if proc else base)

        # Platforms
        platforms = list({m.get("platform") or m.get("source_name") for m in mentions if m.get("platform") or m.get("source_name")})[:4]

        avg_impact = round(sum(m.get("impact_score") or 0 for m in mentions) / len(mentions))

        nodes.append({
            "id":                key,
            "title":             title,
            "narrative_type":    nt,
            "severity":          severity,
            "count":             len(mentions),
            "avg_impact":        avg_impact,
            "velocity":          velocity,
            "velocity_trend":    trend,
            "platforms":         platforms,
            "top_mention_id":    top["id"],
            "ai_summary":        top.get("ai_summary") or "",
            "why_it_matters":    top.get("why_it_matters") or "",
            "recommended_action":top.get("recommended_action") or "",
            "needs_legal":       any(m.get("needs_legal_review") for m in mentions),
            "needs_outreach":    any(m.get("patient_outreach_needed") for m in mentions),
            "related_location":  top.get("related_location") or "",
            "related_procedure": top.get("related_procedure") or "",
        })

    # Sort: critical first, then by count
    nodes.sort(key=lambda n: (-SEVERITY_RANK.get(n["severity"], 1), -n["count"]))
    return jsonify(nodes)

@app.route("/")
def dashboard():
    stats = get_stats()
    with get_db() as conn:
        # Fallback highest-impact list (shown when no priority queues have items)
        mentions = rows_to_list(conn.execute("""
            SELECT id, title, platform, source_name, sentiment, risk_level,
                   impact_score, status, discovered_at, is_opportunity, is_threat,
                   notify_leadership, needs_legal_review, narrative_type
            FROM mentions
            WHERE status NOT IN ('archived')
            ORDER BY impact_score DESC, discovered_at DESC
            LIMIT 8
        """).fetchall())

        alerts = rows_to_list(conn.execute("""
            SELECT a.*, m.id as mention_id_ref
            FROM alerts a LEFT JOIN mentions m ON a.mention_id = m.id
            WHERE a.read = 0
            ORDER BY a.created_at DESC LIMIT 6
        """).fetchall())

        # Critical items — requires immediate attention
        critical_items = rows_to_list(conn.execute("""
            SELECT id, title, platform, source_name, sentiment, risk_level,
                   impact_score, status, discovered_at, narrative_type
            FROM mentions
            WHERE risk_level='critical' AND status NOT IN ('resolved','archived')
            ORDER BY impact_score DESC, discovered_at DESC
            LIMIT 6
        """).fetchall())

        # Public response queue
        response_queue = rows_to_list(conn.execute("""
            SELECT id, title, platform, source_name, sentiment, risk_level,
                   impact_score, status, discovered_at, narrative_type
            FROM mentions
            WHERE public_response=1 AND status NOT IN ('resolved','archived')
            ORDER BY impact_score DESC, discovered_at DESC
            LIMIT 5
        """).fetchall())

        # Patient outreach queue
        outreach_queue = rows_to_list(conn.execute("""
            SELECT id, title, platform, source_name, sentiment, risk_level,
                   impact_score, status, discovered_at, narrative_type
            FROM mentions
            WHERE patient_outreach_needed=1 AND status NOT IN ('resolved','archived')
            ORDER BY impact_score DESC, discovered_at DESC
            LIMIT 5
        """).fetchall())

        # Narrative breakdown — complaint themes in the last 30 days
        narrative_rows = conn.execute("""
            SELECT narrative_type, COUNT(*) as cnt
            FROM mentions
            WHERE discovered_at >= datetime('now', '-30 days')
              AND sentiment IN ('negative','neutral')
              AND narrative_type IS NOT NULL
            GROUP BY narrative_type
            ORDER BY cnt DESC
        """).fetchall()
        narrative_counts = [(r["narrative_type"], r["cnt"]) for r in narrative_rows]

    for m in mentions:
        m["risk_color"]      = risk_color(m["risk_level"] or "")
        m["sentiment_color"] = sentiment_color(m["sentiment"] or "")

    return render_template("dashboard.html",
        stats=stats, mentions=mentions, alerts=alerts,
        critical_items=critical_items,
        response_queue=response_queue,
        outreach_queue=outreach_queue,
        narrative_counts=narrative_counts,
        narrative_labels=NARRATIVE_LABELS,
    )


@app.route("/mentions")
def mentions_page():
    view      = request.args.get("view", "")
    sentiment = request.args.get("sentiment", "")
    risk      = request.args.get("risk", "")
    source    = request.args.get("source", "")
    status    = request.args.get("status", "")
    search    = request.args.get("search", "")
    sort      = request.args.get("sort", "post_date")
    page      = int(request.args.get("page", 1))
    page_size = 25

    where, params = ["1=1"], []

    if view == "critical":       where.append("risk_level='critical'")
    elif view == "negative":     where.append("sentiment='negative'")
    elif view == "positive":     where.append("sentiment='positive'")
    elif view == "legal":        where.append("needs_legal_review=1")
    elif view == "opportunities":where.append("is_opportunity=1")
    elif view == "new":          where.append("status='new'")
    elif view == "archived":     where.append("status='archived'")
    elif view == "response":     where.append("public_response=1")
    elif view == "outreach":     where.append("patient_outreach_needed=1")

    # Hide archived by default unless explicitly requested
    if view not in ("archived", "response", "outreach") and not status:
        where.append("status != 'archived'")

    if sentiment: where.append("sentiment=?");  params.append(sentiment)
    if risk:      where.append("risk_level=?"); params.append(risk)
    if source:    where.append("source_name=?");params.append(source)
    if status:    where.append("status=?");     params.append(status)
    if search:
        where.append("(title LIKE ? OR snippet LIKE ? OR author LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]

    where_sql = " AND ".join(where)

    # Sort order
    if sort == "post_date":
        order_sql = "CASE WHEN published_at IS NULL OR published_at='' THEN 1 ELSE 0 END, published_at DESC, discovered_at DESC"
    elif sort == "found_date":
        order_sql = "discovered_at DESC"
    else:
        order_sql = "impact_score DESC, discovered_at DESC"

    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM mentions WHERE {where_sql}", params).fetchone()[0]
        rows = rows_to_list(conn.execute(f"""
            SELECT id, title, platform, source_name, author, sentiment, risk_level,
                   impact_score, status, discovered_at, published_at, engagement_count, engagement_label,
                   is_opportunity, is_threat, notify_leadership, needs_legal_review, url
            FROM mentions WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """, params + [page_size, (page - 1) * page_size]).fetchall())

    for m in rows:
        m["risk_color"]      = risk_color(m["risk_level"] or "")
        m["sentiment_color"] = sentiment_color(m["sentiment"] or "")

    stats = get_stats()
    total_pages = max(1, (total + page_size - 1) // page_size)

    return render_template("mentions.html",
        mentions=rows, total=total, page=page, total_pages=total_pages,
        view=view, sentiment=sentiment, risk=risk, source=source,
        status=status, search=search, sort=sort, stats=stats)


@app.route("/mentions/<mid>")
def mention_detail(mid):
    with get_db() as conn:
        m = row_to_dict(conn.execute("SELECT * FROM mentions WHERE id=?", (mid,)).fetchone())
        if not m:
            return "Not found", 404
        m["keywords"] = rows_to_list(conn.execute("""
            SELECT k.phrase, k.category FROM mention_keywords mk
            JOIN keywords k ON mk.keyword_id = k.id WHERE mk.mention_id=?
        """, (mid,)).fetchall())
        m["notes"] = rows_to_list(conn.execute(
            "SELECT * FROM notes WHERE mention_id=? ORDER BY created_at DESC", (mid,)
        ).fetchall())
        m["history"] = rows_to_list(conn.execute(
            "SELECT * FROM status_history WHERE mention_id=? ORDER BY created_at DESC", (mid,)
        ).fetchall())
        m["alerts"] = rows_to_list(conn.execute(
            "SELECT * FROM alerts WHERE mention_id=? ORDER BY created_at DESC", (mid,)
        ).fetchall())

    m["risk_color"]      = risk_color(m["risk_level"] or "")
    m["sentiment_color"] = sentiment_color(m["sentiment"] or "")
    score_factors = json.loads(m["raw_score_factors"]) if m.get("raw_score_factors") else {}
    stats = get_stats()

    return render_template("mention_detail.html", m=m, score_factors=score_factors, stats=stats)


@app.route("/settings")
def settings_page():
    with get_db() as conn:
        keywords = rows_to_list(conn.execute("SELECT * FROM keywords ORDER BY category, phrase").fetchall())
        neg_keywords = rows_to_list(conn.execute("SELECT * FROM negative_keywords ORDER BY phrase").fetchall())
    stats = get_stats()
    return render_template("settings.html", keywords=keywords, neg_keywords=neg_keywords, stats=stats)


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """
    Kicks off a non-blocking background scan and returns immediately.
    Poll /api/scan/status to track progress.
    """
    global _scan_state
    with _scan_lock:
        if _scan_state["running"]:
            return jsonify({"running": True, "message": "Scan already in progress"}), 202

        _scan_state = {"running": True, "new_count": 0, "error": None}

    def _bg():
        global _scan_state
        try:
            result = run_scan()
            with _scan_lock:
                _scan_state = {"running": False, "new_count": result.get("new_count", 0), "error": None}
        except Exception as e:
            with _scan_lock:
                _scan_state = {"running": False, "new_count": 0, "error": str(e)}

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/scan/status", methods=["GET"])
def api_scan_status():
    """Returns current scan progress. Poll this while scan-bar is visible."""
    with _scan_lock:
        state = dict(_scan_state)
    state["stats"] = get_stats()
    return jsonify(state)


@app.route("/api/mentions/bulk-classify", methods=["POST"])
def bulk_classify():
    """Classify a batch of mentions into a label (critical/negative/positive/legal/opportunity/archived)."""
    body  = request.get_json(force=True) or {}
    ids   = body.get("ids", [])
    label = body.get("label", "").lower()
    if not ids or not label:
        return jsonify({"error": "ids and label required"}), 400

    # Map label → field updates
    CLASSIFY_MAP = {
        "critical":    {"risk_level": "critical"},
        "negative":    {"sentiment": "negative"},
        "positive":    {"sentiment": "positive"},
        "legal":       {"needs_legal_review": 1},
        "opportunity": {"is_opportunity": 1},
        "archived":    {"status": "archived"},
    }
    updates = CLASSIFY_MAP.get(label)
    if not updates:
        return jsonify({"error": f"Unknown label '{label}'"}), 400

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values     = list(updates.values())
    placeholders = ",".join("?" * len(ids))

    with get_db() as conn:
        conn.execute(
            f"UPDATE mentions SET {set_clause}, updated_at=datetime('now') WHERE id IN ({placeholders})",
            values + ids,
        )
        conn.commit()

    return jsonify({"ok": True, "updated": len(ids), "label": label})


@app.route("/api/mentions/bulk-status", methods=["POST"])
def api_bulk_status():
    """Set status on multiple mentions at once. Body: {ids: [...], status: 'archived'}"""
    data   = request.json or {}
    ids    = data.get("ids", [])
    status = data.get("status", "archived")
    if not ids:
        return jsonify({"error": "no ids"}), 400
    allowed = {"new", "reviewing", "escalated", "resolved", "ignored", "archived"}
    if status not in allowed:
        return jsonify({"error": "invalid status"}), 400
    placeholders = ",".join("?" * len(ids))
    with get_db() as conn:
        updated = conn.execute(
            f"UPDATE mentions SET status=?, updated_at=datetime('now') WHERE id IN ({placeholders})",
            [status] + ids
        ).rowcount
        conn.commit()
    return jsonify({"updated": updated})


@app.route("/api/debug-scan")
def api_debug_scan():
    """
    Runs ONE connector for ONE keyword and returns raw results.
    Use this to diagnose live scan issues without touching the DB.
    Visit: /api/debug-scan
    """
    import traceback
    out = {}

    # 1. Test outbound HTTP
    try:
        import requests as _req
        r = _req.get("https://news.google.com/rss/search?q=goals+plastic+surgery&hl=en-US&gl=US&ceid=US:en", timeout=10)
        out["http_status"] = r.status_code
        out["http_bytes"]  = len(r.content)
    except Exception as e:
        out["http_error"] = str(e)

    # 2. Test Google News RSS connector
    try:
        from connectors.google_news_rss import search_google_news_rss
        results = search_google_news_rss("Goals Plastic Surgery")
        out["gnews_total"] = len(results)
        out["gnews_sample"] = [{"title": r["title"][:80], "snippet": r.get("snippet","")[:60]} for r in results[:3]]
    except Exception as e:
        out["gnews_error"] = traceback.format_exc()

    # 3. Check brand filter
    try:
        from scanner import _is_brand_relevant
        passed = [r for r in results if _is_brand_relevant(r.get("title",""), r.get("snippet",""), r.get("matched_keyword",""))]
        out["brand_filter_pass"] = len(passed)
        out["brand_filter_blocked"] = len(results) - len(passed)
    except Exception as e:
        out["brand_filter_error"] = str(e)

    # 4. Check dedup — how many would be new vs already in DB
    try:
        import re
        def _nu(u): return re.sub(r"[?#].*","",u.rstrip("/"))
        def _tk(t): return re.sub(r"[^a-z0-9 ]","",t.lower())[:60].strip()
        with get_db() as conn:
            existing_urls   = {row[0] for row in conn.execute("SELECT url FROM mentions WHERE url IS NOT NULL").fetchall()}
            existing_titles = {_tk(row[0]) for row in conn.execute("SELECT title FROM mentions").fetchall()}
        new = [r for r in passed if _nu(r.get("url","")) not in existing_urls and _tk(r.get("title","")) not in existing_titles]
        out["dedup_would_be_new"] = len(new)
        out["dedup_blocked"]      = len(passed) - len(new)
        out["dedup_new_sample"]   = [r["title"][:80] for r in new[:5]]
    except Exception as e:
        out["dedup_error"] = str(e)

    # 5. DB path + mention count
    try:
        from db import DB_PATH
        out["db_path"] = DB_PATH
        with get_db() as conn:
            out["db_total_mentions"] = conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    except Exception as e:
        out["db_error"] = str(e)

    return jsonify(out)


@app.route("/api/mentions", methods=["GET", "POST"])
def api_mentions():
    if request.method == "POST":
        data = request.json
        title       = data.get("title", "").strip()
        snippet     = data.get("snippet", "")
        source_name = data.get("source_name", "manual")
        if not title:
            return jsonify({"error": "title required"}), 400

        ai    = analyze_mention(title, snippet)
        score = calculate_score(
            source_name=source_name, sentiment=ai["sentiment"],
            engagement_count=data.get("engagement_count"),
            title=title, snippet=snippet,
            related_surgeon=data.get("related_surgeon"),
            related_location=data.get("related_location"),
            related_procedure=data.get("related_procedure"),
        )
        mid = str(uuid.uuid4())
        with get_db() as conn:
            conn.execute("""
                INSERT INTO mentions
                  (id, url, title, snippet, author, source_name, platform,
                   engagement_count, related_surgeon, related_location, related_procedure,
                   sentiment, risk_level, impact_score, ai_summary, why_it_matters,
                   recommended_action, notify_leadership, needs_legal_review, public_response,
                   is_opportunity, is_threat, raw_score_factors, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                mid, data.get("url"), title, snippet, data.get("author"),
                source_name, data.get("platform", source_name),
                data.get("engagement_count"),
                data.get("related_surgeon"), data.get("related_location"), data.get("related_procedure"),
                ai["sentiment"], score["risk_level"], score["impact_score"],
                ai["ai_summary"], ai["why_it_matters"], ai["recommended_action"],
                int(score["notify_leadership"]), int(score["needs_legal_review"]),
                int(ai.get("public_response", False)),
                int(score["is_opportunity"]), int(score["is_threat"]),
                json.dumps(score["score_factors"]), "new",
            ))
            if score["risk_level"] in ("critical", "high"):
                conn.execute("""
                    INSERT INTO alerts (id, mention_id, type, title, body, severity)
                    VALUES (?,?,?,?,?,?)
                """, (str(uuid.uuid4()), mid,
                      "critical_mention" if score["risk_level"] == "critical" else "high_risk",
                      f"{'Critical' if score['risk_level'] == 'critical' else 'High Risk'}: {title[:70]}",
                      ai["ai_summary"], score["risk_level"]))
            conn.commit()
        return jsonify({"id": mid, "impact_score": score["impact_score"], "risk_level": score["risk_level"]}), 201

    # GET
    return jsonify({"message": "Use the web UI for mention listing"}), 200


@app.route("/api/mentions/<mid>", methods=["PATCH"])
def api_mention_update(mid):
    data = request.json
    allowed = ["status", "assigned_to", "needs_legal_review", "notify_leadership",
               "public_response", "is_opportunity", "is_threat"]

    with get_db() as conn:
        current = conn.execute("SELECT * FROM mentions WHERE id=?", (mid,)).fetchone()
        if not current:
            return jsonify({"error": "Not found"}), 404

        updates, params = [], []
        for key in allowed:
            if key in data:
                updates.append(f"{key}=?")
                params.append(data[key])

        if updates:
            params.append(mid)
            conn.execute(f"UPDATE mentions SET {', '.join(updates)}, updated_at=datetime('now') WHERE id=?", params)

        if "status" in data and data["status"] != current["status"]:
            conn.execute("""
                INSERT INTO status_history (id, mention_id, from_status, to_status, note)
                VALUES (?,?,?,?,?)
            """, (str(uuid.uuid4()), mid, current["status"], data["status"], data.get("note")))

        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/mentions/<mid>/reanalyze", methods=["POST"])
def api_reanalyze(mid):
    """Force-rerun AI analysis on a single mention (bypasses batch cap)."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM mentions WHERE id=?", (mid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        m = dict(row)

    ai = analyze_mention(m["title"], m.get("snippet") or "", force=True)
    score = calculate_score(
        source_name=m["source_name"],
        sentiment=ai["sentiment"],
        engagement_count=m.get("engagement_count"),
        published_at=m.get("published_at"),
        title=m["title"],
        snippet=m.get("snippet") or "",
        related_surgeon=m.get("related_surgeon"),
        related_location=m.get("related_location"),
        related_procedure=m.get("related_procedure"),
        narrative_type=ai.get("narrative_type"),
    )

    with get_db() as conn:
        conn.execute("""
            UPDATE mentions SET
              sentiment=?, risk_level=?, impact_score=?,
              narrative_type=?, ai_summary=?, why_it_matters=?,
              recommended_action=?, response_draft=?,
              patient_outreach_needed=?, public_response=?,
              notify_leadership=?, needs_legal_review=?,
              is_opportunity=?, is_threat=?,
              raw_score_factors=?, ai_used=1,
              updated_at=datetime('now')
            WHERE id=?
        """, (
            ai["sentiment"], score["risk_level"], score["impact_score"],
            ai.get("narrative_type", "general_mention"),
            ai["ai_summary"], ai["why_it_matters"], ai["recommended_action"],
            ai.get("response_draft", ""),
            int(ai.get("patient_outreach_needed", False)),
            int(ai.get("public_response", False)),
            int(score["notify_leadership"]), int(score["needs_legal_review"]),
            int(score["is_opportunity"]), int(score["is_threat"]),
            json.dumps(score["score_factors"]),
            mid,
        ))
        conn.commit()

    return jsonify({
        "ok": True,
        "sentiment":   ai["sentiment"],
        "risk_level":  score["risk_level"],
        "impact_score":score["impact_score"],
        "ai_summary":  ai["ai_summary"],
        "why_it_matters": ai["why_it_matters"],
        "recommended_action": ai["recommended_action"],
    })


@app.route("/api/mentions/<mid>/notes", methods=["POST"])
def api_add_note(mid):
    data = request.json
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content required"}), 400
    with get_db() as conn:
        conn.execute(
            "INSERT INTO notes (id, mention_id, content, is_privileged) VALUES (?,?,?,?)",
            (str(uuid.uuid4()), mid, content, int(data.get("is_privileged", 0)))
        )
        conn.commit()
    return jsonify({"ok": True}), 201


@app.route("/api/alerts", methods=["GET", "PATCH"])
def api_alerts():
    if request.method == "PATCH":
        data = request.json
        with get_db() as conn:
            if data.get("mark_all_read"):
                conn.execute("UPDATE alerts SET read=1 WHERE read=0")
            elif data.get("id"):
                conn.execute("UPDATE alerts SET read=1 WHERE id=?", (data["id"],))
            conn.commit()
        return jsonify({"ok": True})

    with get_db() as conn:
        alerts = rows_to_list(conn.execute("""
            SELECT a.*, m.id as mention_page_id
            FROM alerts a LEFT JOIN mentions m ON a.mention_id = m.id
            ORDER BY a.created_at DESC LIMIT 50
        """).fetchall())
    return jsonify(alerts)


@app.route("/api/keywords", methods=["GET", "POST", "PATCH", "DELETE"])
def api_keywords():
    with get_db() as conn:
        if request.method == "GET":
            return jsonify(rows_to_list(conn.execute("SELECT * FROM keywords ORDER BY category, phrase").fetchall()))

        data = request.json
        if request.method == "POST":
            phrase   = (data.get("phrase") or "").strip()
            category = data.get("category", "brand")
            if not phrase:
                return jsonify({"error": "phrase required"}), 400
            try:
                conn.execute(
                    "INSERT INTO keywords (id, phrase, category) VALUES (?,?,?)",
                    (str(uuid.uuid4()), phrase, category)
                )
                conn.commit()
                return jsonify({"ok": True}), 201
            except Exception:
                return jsonify({"error": "Keyword already exists"}), 409

        if request.method == "PATCH":
            kid = data.get("id")
            updates, params = [], []
            for field in ("phrase", "category", "enabled"):
                if field in data:
                    updates.append(f"{field}=?")
                    params.append(data[field])
            if updates:
                conn.execute(f"UPDATE keywords SET {', '.join(updates)} WHERE id=?", params + [kid])
                conn.commit()
            return jsonify({"ok": True})

        if request.method == "DELETE":
            conn.execute("DELETE FROM keywords WHERE id=?", (data.get("id"),))
            conn.commit()
            return jsonify({"ok": True})


@app.route("/api/negative-keywords", methods=["GET", "POST", "DELETE"])
def api_negative_keywords():
    with get_db() as conn:
        if request.method == "GET":
            return jsonify(rows_to_list(conn.execute(
                "SELECT * FROM negative_keywords ORDER BY phrase"
            ).fetchall()))

        data = request.json
        if request.method == "POST":
            phrase = (data.get("phrase") or "").strip().lower()
            if not phrase:
                return jsonify({"error": "phrase required"}), 400
            try:
                conn.execute(
                    "INSERT INTO negative_keywords (id, phrase) VALUES (?,?)",
                    (str(uuid.uuid4()), phrase)
                )
                conn.commit()
                return jsonify({"ok": True}), 201
            except Exception:
                return jsonify({"error": "Already exists"}), 409

        if request.method == "DELETE":
            conn.execute("DELETE FROM negative_keywords WHERE id=?", (data.get("id"),))
            conn.commit()
            return jsonify({"ok": True})


@app.route("/api/keywords/suggest")
def api_keyword_suggestions():
    """
    Extract frequently co-occurring n-gram phrases from existing mention titles/snippets.
    Returns ranked suggestions the user can promote to tracked keywords.
    Excludes: already-tracked keywords, dismissed suggestions, stop-word-only grams.
    """
    import re
    from collections import defaultdict

    STOP_WORDS = {
        "a","an","the","is","in","on","at","to","for","of","and","or","but",
        "not","with","this","that","it","its","are","was","were","be","been",
        "being","have","has","had","do","does","did","will","would","could",
        "should","may","might","shall","can","from","by","as","if","then",
        "than","so","yet","my","your","his","her","our","their","we","they",
        "he","she","i","me","him","us","them","who","what","which","when",
        "where","why","how","all","each","every","about","after","before",
        "into","through","during","up","down","out","over","under","again",
        "no","very","just","also","new","one","two","three","more","said",
        "says","say","like","make","made","know","see","get","got","go",
        "want","need","now","still","even","many","much","some","such",
        "same","other","another","first","last","long","great","little",
        "good","bad","here","there","these","those","using","used","via",
        "per","due","vs","re","am","pm","since","after","before","during",
        "between","among","within","without","against","along","around",
        "behind","beside","beyond","inside","outside","until","upon",
        # HTML artifacts & junk
        "nbsp","amp","quot","apos","lt","gt","http","https","www","com",
        "html","php","review","reviews","posted","post","comment","comments",
        "read","reading","article","page","site","link","click","here",
        "open","view","watch","share","portal","newswire","cnj",
    }

    def _clean_text(text: str) -> str:
        """Strip HTML tags, decode common entities, normalize whitespace."""
        text = re.sub(r"<[^>]+>", " ", text)                   # strip tags
        text = re.sub(r"&[a-zA-Z]+;", " ", text)               # named entities
        text = re.sub(r"&#\d+;", " ", text)                    # numeric entities
        text = re.sub(r"\s+", " ", text).strip()
        return text

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, snippet FROM mentions WHERE title IS NOT NULL"
        ).fetchall()

        existing = {
            row[0].lower().strip()
            for row in conn.execute("SELECT phrase FROM keywords").fetchall()
        }

        dismissed = {
            row[0] for row in conn.execute(
                "SELECT phrase FROM dismissed_suggestions"
            ).fetchall()
        }

    # phrase → set of mention IDs that contain it
    phrase_mentions: dict = defaultdict(set)

    for row in rows:
        mid   = row["id"]
        title = _clean_text(row["title"] or "")
        snip  = _clean_text(row["snippet"] or "")

        # Titles are more signal-dense — process them twice
        for text in (title, title, snip):
            # Pull clean word tokens (preserve original casing for display)
            tokens = re.findall(r"[A-Za-z][a-zA-Z']*", text)
            lowers = [t.lower() for t in tokens]
            n_tok  = len(tokens)

            for n in (2, 3, 4):
                for i in range(n_tok - n + 1):
                    chunk_lower = lowers[i:i + n]
                    chunk_orig  = tokens[i:i + n]

                    # Must not start or end on a stop word
                    if chunk_lower[0] in STOP_WORDS or chunk_lower[-1] in STOP_WORDS:
                        continue

                    # Need at least one content word longer than 3 chars
                    content = [w for w in chunk_lower
                               if w not in STOP_WORDS and len(w) > 3]
                    if not content:
                        continue

                    phrase_lower = " ".join(chunk_lower)
                    phrase_orig  = " ".join(chunk_orig)

                    # Skip if already tracked or dismissed
                    if phrase_lower in existing or phrase_lower in dismissed:
                        continue

                    # Skip if entirely covered by an existing keyword
                    # (e.g. "Plastic Surgery" when "Goals Plastic Surgery" is tracked)
                    if any(phrase_lower in ex for ex in existing):
                        continue

                    # Store with original casing (first seen wins for display)
                    key = phrase_lower
                    if key not in phrase_mentions:
                        phrase_mentions[key] = {"ids": set(), "display": phrase_orig}
                    phrase_mentions[key]["ids"].add(mid)

    # Score: unique mention count; require ≥ 2 distinct mentions
    suggestions = []
    for phrase_lower, data in phrase_mentions.items():
        count = len(data["ids"])
        if count < 2:
            continue
        suggestions.append({
            "phrase":        data["display"],
            "phrase_lower":  phrase_lower,
            "mention_count": count,
        })

    suggestions.sort(key=lambda x: -x["mention_count"])
    return jsonify(suggestions[:40])


@app.route("/api/keywords/suggest/dismiss", methods=["POST"])
def api_dismiss_suggestion():
    """Persist a dismissed suggestion so it won't resurface."""
    data   = request.json or {}
    phrase = (data.get("phrase") or "").strip().lower()
    if not phrase:
        return jsonify({"error": "phrase required"}), 400
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO dismissed_suggestions (phrase) VALUES (?)", (phrase,)
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/data/reset", methods=["POST"])
def api_data_reset():
    """Wipe all scanned mentions, alerts, and scan logs. Keeps keywords and manual mentions."""
    with get_db() as conn:
        deleted = conn.execute(
            "DELETE FROM mentions WHERE source_name != 'manual'"
        ).rowcount
        conn.execute("DELETE FROM alerts")
        conn.execute("DELETE FROM scan_logs")
        conn.commit()
    return jsonify({"ok": True, "deleted": deleted})


# ── Google Business Profile OAuth + sync ─────────────────────────────────────

@app.route("/api/integrations/google/auth")
def google_auth():
    """Start the Google OAuth flow — redirect user to Google consent screen."""
    try:
        from connectors.google_business_profile import get_oauth_url
        return redirect(get_oauth_url())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/integrations/google/callback")
def google_callback():
    """Handle Google OAuth callback — exchange code for tokens then redirect to settings."""
    code  = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        return redirect("/settings?google_error=" + (error or "no_code"))
    try:
        from connectors.google_business_profile import (
            exchange_code, store_tokens, list_accounts, list_locations
        )
        tokens = exchange_code(code)
        store_tokens(
            tokens["access_token"],
            tokens.get("refresh_token"),
            tokens.get("expires_in", 3600),
        )
        # Discover and store locations automatically
        try:
            accounts = list_accounts()
            with get_db() as conn:
                for account in accounts:
                    account_name = account.get("name", "")
                    try:
                        locs = list_locations(account_name)
                        for loc in locs:
                            conn.execute("""
                                INSERT OR IGNORE INTO google_locations (id, name, title, account_name)
                                VALUES (?, ?, ?, ?)
                            """, (str(uuid.uuid4()), loc.get("name", ""),
                                  loc.get("title", ""), account_name))
                    except Exception as le:
                        print(f"[GBP] Location fetch error for {account_name}: {le}")
                conn.commit()
        except Exception as ae:
            print(f"[GBP] Account discovery error: {ae}")
        return redirect("/settings?google_connected=1")
    except Exception as e:
        print(f"[GBP] OAuth callback error: {e}")
        return redirect("/settings?google_error=token_exchange_failed")


@app.route("/api/integrations/google/status")
def google_status():
    """Return connection status, last sync time, and location list."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT connected, updated_at FROM integrations WHERE service='google_business'"
        ).fetchone()
        locations = [dict(r) for r in conn.execute(
            "SELECT name, title, address, last_synced FROM google_locations"
        ).fetchall()]
    return jsonify({
        "connected":  bool(row and row["connected"]),
        "updated_at": row["updated_at"] if row else None,
        "locations":  locations,
    })


@app.route("/api/integrations/google/sync", methods=["POST"])
def google_sync():
    """Trigger a review sync for all connected locations."""
    try:
        from connectors.google_business_profile import sync_all_locations
        results   = sync_all_locations()
        total_new = sum(v for v in results.values() if isinstance(v, int))
        return jsonify({"ok": True, "results": results, "total_new": total_new})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/integrations/google/disconnect", methods=["POST"])
def google_disconnect():
    """Revoke stored Google tokens."""
    with get_db() as conn:
        conn.execute("""
            UPDATE integrations
            SET connected=0, access_token=NULL, refresh_token=NULL
            WHERE service='google_business'
        """)
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/integrations/google/reply/<path:review_name>", methods=["POST"])
def google_reply(review_name):
    """Post or update a reply to a specific Google review."""
    body       = request.get_json(force=True)
    reply_text = body.get("reply_text", "").strip()
    if not reply_text:
        return jsonify({"error": "reply_text required"}), 400
    try:
        from connectors.google_business_profile import reply_to_review
        ok = reply_to_review(review_name, reply_text)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Firecrawl — deep page indexing ────────────────────────────────────────────

@app.route("/api/mentions/<mid>/deep-index", methods=["POST"])
def mention_deep_index(mid):
    """Manually trigger Firecrawl deep-indexing for a mention."""
    if not os.getenv("FIRECRAWL_API_KEY"):
        return jsonify({"error": "FIRECRAWL_API_KEY not configured"}), 400
    try:
        from connectors.firecrawl_connector import deep_index_mention
        ok = deep_index_mention(mid)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mentions/<mid>/context")
def mention_context(mid):
    """Return stored Firecrawl context for a mention."""
    try:
        from connectors.firecrawl_connector import get_mention_context
        ctx = get_mention_context(mid)
        return jsonify(ctx or {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/integrations/firecrawl/batch", methods=["POST"])
def firecrawl_batch():
    """Batch deep-index all high-risk mentions that haven't been indexed yet."""
    if not os.getenv("FIRECRAWL_API_KEY"):
        return jsonify({"error": "FIRECRAWL_API_KEY not configured"}), 400

    body  = request.get_json(force=True) or {}
    limit = int(body.get("limit", 20))

    def _run():
        from connectors.firecrawl_connector import batch_deep_index_high_risk
        batch_deep_index_high_risk(limit=limit)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": f"Batch deep-index started (limit={limit})"})


@app.route("/api/integrations/firecrawl/status")
def firecrawl_status():
    """Return Firecrawl integration status and counts."""
    configured = bool(os.getenv("FIRECRAWL_API_KEY"))
    with get_db() as conn:
        indexed = conn.execute(
            "SELECT COUNT(*) FROM mentions WHERE deep_indexed=1"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM mentions WHERE deep_indexed=0 AND url IS NOT NULL AND impact_score >= 55"
        ).fetchone()[0]
    return jsonify({"configured": configured, "indexed": indexed, "pending_high_risk": pending})


# ── Apify — social scraping ───────────────────────────────────────────────────

@app.route("/api/integrations/apify/run", methods=["POST"])
def apify_run():
    """Start an Apify actor run for a platform + keyword."""
    if not os.getenv("APIFY_API_TOKEN"):
        return jsonify({"error": "APIFY_API_TOKEN not configured"}), 400

    body     = request.get_json(force=True) or {}
    platform = body.get("platform", "").strip().lower()
    keyword  = body.get("keyword", "").strip()
    actor_id = body.get("actor_id")

    if not platform or not keyword:
        return jsonify({"error": "platform and keyword required"}), 400

    try:
        from connectors.apify_connector import start_actor_run, poll_and_ingest_job
        job = start_actor_run(platform, keyword, actor_id=actor_id)

        # Poll + ingest in background thread
        def _poll(jid):
            poll_and_ingest_job(jid)

        threading.Thread(target=_poll, args=(job["job_id"],), daemon=True).start()
        return jsonify({"ok": True, **job})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/integrations/apify/jobs")
def apify_jobs():
    """Return recent Apify jobs."""
    with get_db() as conn:
        jobs = [dict(r) for r in conn.execute("""
            SELECT id, run_id, actor, keyword, platform, status,
                   items_count, started_at, completed_at
            FROM apify_jobs
            ORDER BY started_at DESC
            LIMIT 50
        """).fetchall()]
    return jsonify(jobs)


@app.route("/api/integrations/apify/jobs/<job_id>")
def apify_job_detail(job_id):
    """Return a single Apify job row."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM apify_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@app.route("/api/integrations/apify/status")
def apify_status():
    """Return Apify integration status and job counts."""
    configured = bool(os.getenv("APIFY_API_TOKEN"))
    with get_db() as conn:
        total     = conn.execute("SELECT COUNT(*) FROM apify_jobs").fetchone()[0]
        completed = conn.execute("SELECT COUNT(*) FROM apify_jobs WHERE status='completed'").fetchone()[0]
        running   = conn.execute("SELECT COUNT(*) FROM apify_jobs WHERE status='running'").fetchone()[0]
    return jsonify({"configured": configured, "total_jobs": total,
                    "completed": completed, "running": running})


# ── Local dev entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🚀 Goals PR Impact Monitor")
    print("   http://127.0.0.1:5001\n")
    app.run(debug=True, port=5001, host="127.0.0.1")
