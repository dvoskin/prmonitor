import sqlite3
import os
from datetime import datetime

# On Render: set DB_PATH env var to /data/pr_monitor.db (persistent disk mount)
# Locally: defaults to project directory
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "pr_monitor.db"))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS keywords (
            id          TEXT PRIMARY KEY,
            phrase      TEXT UNIQUE NOT NULL,
            category    TEXT DEFAULT 'brand',
            enabled     INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS mentions (
            id                TEXT PRIMARY KEY,
            url               TEXT UNIQUE,
            title             TEXT NOT NULL,
            snippet           TEXT,
            full_text         TEXT,
            author            TEXT,
            source_name       TEXT NOT NULL,
            platform          TEXT,
            published_at      TEXT,
            discovered_at     TEXT DEFAULT (datetime('now')),
            engagement_count  INTEGER,
            engagement_label  TEXT,
            related_surgeon   TEXT,
            related_location  TEXT,
            related_procedure TEXT,
            screenshot_url    TEXT,
            status            TEXT DEFAULT 'new',
            assigned_to       TEXT,
            sentiment         TEXT,
            risk_level        TEXT,
            impact_score      INTEGER,
            ai_summary        TEXT,
            why_it_matters    TEXT,
            recommended_action TEXT,
            notify_leadership INTEGER DEFAULT 0,
            needs_legal_review INTEGER DEFAULT 0,
            public_response   INTEGER DEFAULT 0,
            is_opportunity    INTEGER DEFAULT 0,
            is_threat         INTEGER DEFAULT 0,
            raw_score_factors TEXT,
            created_at        TEXT DEFAULT (datetime('now')),
            updated_at        TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS mention_keywords (
            mention_id TEXT NOT NULL,
            keyword_id TEXT NOT NULL,
            PRIMARY KEY (mention_id, keyword_id),
            FOREIGN KEY (mention_id) REFERENCES mentions(id) ON DELETE CASCADE,
            FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id          TEXT PRIMARY KEY,
            mention_id  TEXT,
            type        TEXT,
            title       TEXT,
            body        TEXT,
            severity    TEXT,
            read        INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (mention_id) REFERENCES mentions(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS notes (
            id           TEXT PRIMARY KEY,
            mention_id   TEXT NOT NULL,
            content      TEXT NOT NULL,
            is_privileged INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (mention_id) REFERENCES mentions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS status_history (
            id          TEXT PRIMARY KEY,
            mention_id  TEXT NOT NULL,
            from_status TEXT,
            to_status   TEXT,
            note        TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (mention_id) REFERENCES mentions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scan_logs (
            id                 TEXT PRIMARY KEY,
            scanned_at         TEXT DEFAULT (datetime('now')),
            new_mentions_count INTEGER DEFAULT 0,
            total_scanned      INTEGER DEFAULT 0,
            sources_scanned    TEXT DEFAULT '[]',
            status             TEXT,
            error_message      TEXT,
            duration_ms        INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_mentions_impact ON mentions(impact_score DESC);
        CREATE INDEX IF NOT EXISTS idx_mentions_status ON mentions(status);
        CREATE INDEX IF NOT EXISTS idx_mentions_sentiment ON mentions(sentiment);
        CREATE INDEX IF NOT EXISTS idx_mentions_risk ON mentions(risk_level);
        CREATE INDEX IF NOT EXISTS idx_alerts_read ON alerts(read);
        """)


def seed_db():
    import uuid
    from ranker import calculate_score

    with get_db() as conn:
        # Keywords
        keywords = [
            ("Goals Plastic Surgery", "brand"),
            ("Goals Aesthetics", "brand"),
            ("Goals Plastic Surgery reviews", "brand"),
            ("Goals Plastic Surgery lawsuit", "brand"),
            ("Goals Plastic Surgery complaints", "brand"),
            ("Goals Plastic Surgery BBL", "procedure"),
            ("Goals Plastic Surgery liposuction", "procedure"),
            ("FlexSculpt", "procedure"),
            ("DoubleBBL", "procedure"),
            ("Dr. Sergey Voskin", "person"),
            ("Goals Plastic Surgery Houston", "location"),
            ("Goals Plastic Surgery New York", "location"),
            ("Goals Plastic Surgery Atlanta", "location"),
            ("Goals Plastic Surgery Los Angeles", "location"),
            ("Goals Plastic Surgery Philadelphia", "location"),
            ("Goals Plastic Surgery Miami", "location"),
            ("Goals Plastic Surgery reviews Reddit", "brand"),
            ("Goals Plastic Surgery TikTok", "brand"),
        ]
        for phrase, cat in keywords:
            conn.execute(
                "INSERT OR IGNORE INTO keywords (id, phrase, category) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), phrase, cat)
            )

        # Sample mentions
        samples = [
            {
                "title": "Goals Plastic Surgery Review – My BBL Experience (5 Stars)",
                "url": "https://www.realself.com/review/goals-bbl-5stars",
                "snippet": "Had my BBL at Goals Plastic Surgery in Miami last month. Dr. Voskin was incredible — very professional, answered all my questions, and the results are beyond what I imagined. The staff made me feel safe throughout. 10/10 would recommend.",
                "source_name": "realself", "platform": "RealSelf",
                "sentiment": "positive", "risk_level": "low", "impact_score": 22,
                "related_surgeon": "Dr. Sergey Voskin", "related_location": "Miami", "related_procedure": "BBL",
                "ai_summary": "Highly positive patient review praising Dr. Voskin and the Miami location for a BBL procedure.",
                "why_it_matters": "Strong social proof on a high-authority review platform. Good candidate for testimonial repurposing.",
                "recommended_action": "Flag as positive opportunity. Consider reaching out for a video testimonial.",
                "is_opportunity": 1, "notify_leadership": 0, "needs_legal_review": 0,
                "status": "new", "engagement_count": 47, "engagement_label": "helpful votes",
            },
            {
                "title": "Warning: My Experience at Goals Plastic Surgery Was a Nightmare",
                "url": "https://www.reddit.com/r/PlasticSurgery/comments/goals_warning",
                "snippet": "I had a liposuction procedure at Goals New York and ended up with an infection that required hospitalization. They refused to take responsibility. I am now consulting a lawyer. Please do your research before going here.",
                "source_name": "reddit", "platform": "Reddit r/PlasticSurgery",
                "sentiment": "negative", "risk_level": "critical", "impact_score": 91,
                "related_location": "New York", "related_procedure": "Liposuction",
                "ai_summary": "Patient alleging post-surgical infection and legal action after liposuction at the New York location.",
                "why_it_matters": "Patient safety claim combined with legal threat on a high-engagement platform. Reddit posts index on Google quickly.",
                "recommended_action": "Escalate to legal immediately. Do not respond publicly without legal clearance. Monitor thread engagement.",
                "notify_leadership": 1, "needs_legal_review": 1, "is_threat": 1,
                "status": "new", "engagement_count": 312, "engagement_label": "upvotes",
            },
            {
                "title": "Goals Plastic Surgery Named in $2M Malpractice Suit – Court Filing",
                "url": "https://www.courthousenews.com/goals-malpractice-filing",
                "snippet": "A federal filing in the Southern District of New York lists Goals Plastic Surgery as a defendant in a malpractice suit involving alleged negligence during a cosmetic procedure performed in 2023.",
                "source_name": "google_news", "platform": "Google News / Legal",
                "sentiment": "negative", "risk_level": "critical", "impact_score": 98,
                "related_location": "New York",
                "ai_summary": "Federal malpractice lawsuit naming Goals Plastic Surgery as defendant, covered by a legal news outlet.",
                "why_it_matters": "Court filings are public record and will index on Google. High likelihood of media pickup.",
                "recommended_action": "Immediate legal team engagement. Prepare media holding statement. Monitor for press pickup.",
                "notify_leadership": 1, "needs_legal_review": 1, "is_threat": 1,
                "status": "escalated", "engagement_count": 89, "engagement_label": "shares",
            },
            {
                "title": "I got a FlexSculpt at Goals and I am OBSESSED 😍 | TikTok",
                "url": "https://www.tiktok.com/@beautybybri/goals-flexsculpt",
                "snippet": "This is my 6-week post-op update after my FlexSculpt at Goals Plastic Surgery. I am literally in shock at my results. If you are on the fence — just do it. Link in bio for my full recovery journey.",
                "source_name": "tiktok", "platform": "TikTok",
                "sentiment": "positive", "risk_level": "moderate", "impact_score": 61,
                "related_procedure": "FlexSculpt",
                "ai_summary": "Influencer-style TikTok with strong positive sentiment about FlexSculpt results at Goals.",
                "why_it_matters": "High-visibility social content with strong organic reach. Potential for significant positive brand exposure.",
                "recommended_action": "Flag as PR opportunity. Contact creator for partnership or repost rights.",
                "is_opportunity": 1, "notify_leadership": 0, "needs_legal_review": 0,
                "status": "reviewing", "engagement_count": 84200, "engagement_label": "views",
            },
            {
                "title": "Goals Plastic Surgery Houston – Is It Worth It? | Reddit Thread",
                "url": "https://www.reddit.com/r/PlasticSurgery/goals_houston_review",
                "snippet": "Mixed reviews here. Some people had great experiences and others complained about post-op follow-up. Staff is nice but scheduling is chaotic. Results were good for me personally but I heard horror stories from others in the waiting room.",
                "source_name": "reddit", "platform": "Reddit r/PlasticSurgery",
                "sentiment": "neutral", "risk_level": "moderate", "impact_score": 38,
                "related_location": "Houston",
                "ai_summary": "Mixed Reddit thread about the Houston location. Positive on results but flags scheduling and post-op follow-up concerns.",
                "why_it_matters": "Neutral-negative thread ranking in Google for Houston location searches. Operational feedback worth addressing.",
                "recommended_action": "Share with operations team for Houston location review. Monitor for reply activity.",
                "notify_leadership": 0, "needs_legal_review": 0,
                "status": "new", "engagement_count": 156, "engagement_label": "upvotes",
            },
            {
                "title": "Dr. Sergey Voskin Discusses BBL Safety on The Aesthetic Hour Podcast",
                "url": "https://aesthetichour.com/episode/dr-voskin-bbl-safety",
                "snippet": "Dr. Sergey Voskin joins host Dr. Lisa Park to discuss evolving BBL safety protocols, the DoubleBBL technique, and how Goals Plastic Surgery has built national scale without compromising outcomes.",
                "source_name": "podcast", "platform": "Podcast / Web",
                "sentiment": "positive", "risk_level": "low", "impact_score": 55,
                "related_surgeon": "Dr. Sergey Voskin", "related_procedure": "BBL",
                "ai_summary": "Positive expert media appearance by Dr. Voskin on a respected aesthetic medicine podcast.",
                "why_it_matters": "Authority-building content from a credible third-party platform. Good for surgeon thought leadership.",
                "recommended_action": "Repurpose clip for social. Link from website. Nominate for additional speaking opportunities.",
                "is_opportunity": 1, "notify_leadership": 0, "needs_legal_review": 0,
                "status": "reviewing", "engagement_count": 4300, "engagement_label": "listens",
            },
            {
                "title": "Goals Plastic Surgery complaints — BBB page shows 14 unresolved",
                "url": "https://www.bbb.org/goals-plastic-surgery",
                "snippet": "The Better Business Bureau profile for Goals Plastic Surgery shows 14 complaints in the past 12 months, of which 9 remain unresolved. Common themes include billing disputes, post-operative care, and scheduling.",
                "source_name": "google", "platform": "BBB / Google",
                "sentiment": "negative", "risk_level": "high", "impact_score": 74,
                "ai_summary": "BBB profile showing 14 complaints in 12 months with 9 unresolved. Billing, post-op care, and scheduling are recurring issues.",
                "why_it_matters": "BBB profiles rank highly in branded Google searches. Unresolved complaints signal operational issues publicly.",
                "recommended_action": "Assign operations team to resolve open complaints. Draft response templates for each category.",
                "notify_leadership": 1, "needs_legal_review": 0, "is_threat": 1,
                "status": "escalated",
            },
        ]

        all_keywords = conn.execute("SELECT id, phrase FROM keywords").fetchall()
        kw_map = {row["phrase"]: row["id"] for row in all_keywords}

        for s in samples:
            existing = conn.execute("SELECT id FROM mentions WHERE url = ?", (s["url"],)).fetchone()
            if existing:
                continue

            mid = str(uuid.uuid4())
            conn.execute("""
                INSERT OR IGNORE INTO mentions
                  (id, url, title, snippet, source_name, platform, sentiment, risk_level,
                   impact_score, ai_summary, why_it_matters, recommended_action,
                   notify_leadership, needs_legal_review, public_response,
                   is_opportunity, is_threat, status, engagement_count, engagement_label,
                   related_surgeon, related_location, related_procedure,
                   published_at, discovered_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','-'||abs(random()%7)||' days'),datetime('now'))
            """, (
                mid, s.get("url"), s["title"], s.get("snippet"),
                s["source_name"], s.get("platform"),
                s.get("sentiment"), s.get("risk_level"), s.get("impact_score"),
                s.get("ai_summary"), s.get("why_it_matters"), s.get("recommended_action"),
                s.get("notify_leadership", 0), s.get("needs_legal_review", 0), s.get("public_response", 0),
                s.get("is_opportunity", 0), s.get("is_threat", 0),
                s.get("status", "new"),
                s.get("engagement_count"), s.get("engagement_label"),
                s.get("related_surgeon"), s.get("related_location"), s.get("related_procedure"),
            ))

            # Link keywords
            text = (s["title"] + " " + (s.get("snippet") or "")).lower()
            for phrase, kid in kw_map.items():
                if phrase.lower() in text:
                    conn.execute(
                        "INSERT OR IGNORE INTO mention_keywords VALUES (?,?)", (mid, kid)
                    )

            # Alerts for high/critical
            if s.get("risk_level") in ("critical", "high"):
                level = s["risk_level"]
                conn.execute("""
                    INSERT INTO alerts (id, mention_id, type, title, body, severity)
                    VALUES (?,?,?,?,?,?)
                """, (
                    str(uuid.uuid4()), mid,
                    "critical_mention" if level == "critical" else "high_risk",
                    f"{'Critical' if level == 'critical' else 'High Risk'}: {s['title'][:70]}",
                    s.get("ai_summary", ""),
                    level,
                ))

        conn.commit()
    print("✅ Database seeded with sample data.")
