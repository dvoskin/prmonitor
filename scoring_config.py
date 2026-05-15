"""
Goals Plastic Surgery — Reputation Impact Scoring Configuration
Tuned specifically for aesthetic medicine reputation dynamics.
Score range: 1–100
"""

# Source authority weights — reflects actual patient-acquisition impact for aesthetics
SOURCE_AUTHORITY = {
    # Highest: directly affects local SEO and patient decisions
    "google":          32,
    "google_reviews":  32,
    # Legal/news = fast media pickup
    "legal":           30,
    "google_news":     28,
    "news":            28,
    # High aesthetic discovery platforms — TikTok/IG virality moves fast
    "tiktok":          26,
    "instagram":       22,
    # Patient review communities — high intent audience
    "realself":        24,
    "healthgrades":    22,
    "yelp":            20,
    "bbb":             20,
    "pissedconsumer":  18,
    # Social platforms — high volume, moderate individual impact
    "reddit":          18,
    "youtube":         16,
    "facebook":        14,
    "x":               14,
    "twitter":         14,
    # Media / editorial
    "bing_news":       24,
    "podcast":         16,
    "blog":            12,
    # Manual entries always scored directly
    "manual":          10,
}

SENTIMENT_MULTIPLIER = {
    "positive": 0.55,   # opportunities score lower so threats stand out
    "neutral":  0.75,
    "negative": 1.0,
}

ENGAGEMENT_THRESHOLDS = [
    (500000, 22),   # viral — over 500k views/likes
    (100000, 19),
    (50000,  16),
    (10000,  13),
    (5000,   10),
    (1000,    7),
    (500,     4),
    (100,     2),
    (0,       1),
]

RECENCY_MULTIPLIER = {
    "today":      1.0,
    "this_week":  0.88,
    "this_month": 0.65,
    "older":      0.38,
}

# Plastic-surgery-specific risk bonuses
RISK_BONUSES = {
    # Medical / legal severity
    "legal_language":         22,  # lawsuit, attorney, malpractice, court
    "patient_safety":         20,  # infection, hospitalized, complication, death
    "regulatory_terms":       16,  # medical board, HIPAA, FDA, investigation
    # Plastic surgery narrative types
    "botched_allegation":     22,  # "botched my bbl", "disfigured", "looks wrong"
    "narrative_payment":      10,  # refund dispute, cash grab, fraud
    "narrative_communication": 8,  # ghosted, no follow-up, abandoned
    # Amplification signals
    "tiktok_source":          10,  # TikTok posts spread into fear narratives fast
    "viral_indicators":       12,  # viral, trending, millions of views, blew up
    # Entity signals
    "surgeon_named":           8,  # surgeon mentioned by name
    "location_named":          5,  # specific location mentioned
    "procedure_named":         3,  # specific procedure mentioned
}

RISK_LEVELS = {
    "low":      (0,  24),
    "moderate": (25, 49),
    "high":     (50, 74),
    "critical": (75, 100),
}

ALERT_THRESHOLDS = {
    "notify_leadership_above": 69,   # lower than generic — aesthetics escalates faster
    "needs_legal_above":       44,
    "flag_threat_above":       44,
}
