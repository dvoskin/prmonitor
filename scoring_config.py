"""
PR Impact Scoring Configuration
Edit this file to tune the scoring model. No restart needed — edit and re-score.
Score range: 1–100
"""

SOURCE_AUTHORITY = {
    "google_news": 30,
    "news":        30,
    "legal":       28,
    "review_site": 20,
    "realself":    20,
    "yelp":        20,
    "bbb":         20,
    "podcast":     18,
    "youtube":     16,
    "tiktok":      15,
    "blog":        14,
    "google":      14,
    "reddit":      12,
    "forum":       10,
    "instagram":   10,
    "facebook":    10,
    "x":           10,
    "twitter":     10,
    "instagram":    8,
    "manual":       5,
}

SENTIMENT_MULTIPLIER = {
    "positive": 0.6,
    "neutral":  0.8,
    "negative": 1.0,
}

ENGAGEMENT_THRESHOLDS = [
    (100000, 20),
    (50000,  17),
    (10000,  14),
    (5000,   11),
    (1000,    8),
    (500,     5),
    (100,     3),
    (0,       1),
]

RECENCY_MULTIPLIER = {
    "today":      1.0,
    "this_week":  0.85,
    "this_month": 0.65,
    "older":      0.40,
}

RISK_BONUSES = {
    "legal_language":   20,  # lawsuit, attorney, malpractice, court, sue
    "patient_safety":   18,  # infection, death, complication, hospitalized
    "regulatory_terms": 15,  # HIPAA, FDA, board of medicine, license
    "viral_indicators": 10,  # viral, trending, millions of views
    "surgeon_named":     8,
    "location_named":    5,
    "procedure_named":   3,
}

RISK_LEVELS = {
    "low":      (0,  24),
    "moderate": (25, 49),
    "high":     (50, 74),
    "critical": (75, 100),
}

ALERT_THRESHOLDS = {
    "notify_leadership_above": 74,
    "needs_legal_above":       49,
    "flag_threat_above":       49,
}
