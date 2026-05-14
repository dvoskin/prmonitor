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
from ranker import calculate_score, risk_color, sentiment_color
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
        opps      = conn.execute("SELECT COUNT(*) FROM mentions WHERE is_opportunity=1").fetchone()[0]
        legal     = conn.execute("SELECT COUNT(*) FROM mentions WHERE needs_legal_review=1 AND status != 'resolved'").fetchone()[0]
        unread    = conn.execute("SELECT COUNT(*) FROM alerts WHERE read=0").fetchone()[0]
        last_scan = conn.execute("SELECT scanned_at, status FROM scan_logs ORDER BY scanned_at DESC LIMIT 1").fetchone()
    return {
        "total": total, "new": new_count, "negative": negative,
        "critical": critical, "opportunities": opps, "legal_queue": legal,
        "unread_alerts": unread,
        "last_scan": last_scan["scanned_at"] if last_scan else None,
        "last_scan_status": last_scan["status"] if last_scan else None,
    }


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    stats = get_stats()
    with get_db() as conn:
        mentions = rows_to_list(conn.execute("""
            SELECT id, title, platform, source_name, sentiment, risk_level,
                   impact_score, status, discovered_at, is_opportunity, is_threat,
                   notify_leadership, needs_legal_review
            FROM mentions
            ORDER BY impact_score DESC, discovered_at DESC
            LIMIT 8
        """).fetchall())

        alerts = rows_to_list(conn.execute("""
            SELECT a.*, m.id as mention_id_ref
            FROM alerts a LEFT JOIN mentions m ON a.mention_id = m.id
            WHERE a.read = 0
            ORDER BY a.created_at DESC LIMIT 6
        """).fetchall())

    for m in mentions:
        m["risk_color"]      = risk_color(m["risk_level"] or "")
        m["sentiment_color"] = sentiment_color(m["sentiment"] or "")

    return render_template("dashboard.html", stats=stats, mentions=mentions, alerts=alerts)


@app.route("/mentions")
def mentions_page():
    view      = request.args.get("view", "")
    sentiment = request.args.get("sentiment", "")
    risk      = request.args.get("risk", "")
    source    = request.args.get("source", "")
    status    = request.args.get("status", "")
    search    = request.args.get("search", "")
    sort      = request.args.get("sort", "score")
    page      = int(request.args.get("page", 1))
    page_size = 25

    where, params = ["1=1"], []

    if view == "critical":       where.append("risk_level='critical'")
    elif view == "negative":     where.append("sentiment='negative'")
    elif view == "positive":     where.append("sentiment='positive'")
    elif view == "legal":        where.append("needs_legal_review=1")
    elif view == "opportunities":where.append("is_opportunity=1")
    elif view == "new":          where.append("status='new'")

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
    stats = get_stats()
    return render_template("settings.html", keywords=keywords, stats=stats)


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


# ── Local dev entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🚀 Goals PR Impact Monitor")
    print("   http://127.0.0.1:5001\n")
    app.run(debug=True, port=5001, host="127.0.0.1")
