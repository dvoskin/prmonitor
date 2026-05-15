"""
Goals Plastic Surgery — Reputation Impact Ranker
Scores mentions specifically for aesthetic medicine reputation dynamics.
"""

import re
from datetime import datetime
import scoring_config as cfg

LEGAL_TERMS  = re.compile(r"lawsuit|attorney|lawyer|malpractice|court|sue\b|sued|suing|settlement|negligence|filing|plaintiff|defendant", re.I)
SAFETY_TERMS = re.compile(r"infection|death|died|complication|hospitali[zs]|icu|sepsis|disfigured|botched|necrosis|bleeding|emergency|scarred", re.I)
BOTCHED_TERMS = re.compile(r"botched|disfigured|asymmetric|lopsided|destroyed|ruined|looks wrong|looks different|uneven|horrible result|bad result", re.I)
REGULATORY   = re.compile(r"hipaa|fda|board of medicine|medical board|licens|investigation|regulatory", re.I)
VIRAL_TERMS  = re.compile(r"going viral|trending|millions of views|blew up|viral|share this|spread the word", re.I)
PAYMENT_TERMS = re.compile(r"refund|cash grab|scam|fraud|overcharged|billing dispute|financing|they kept my money|never refunded", re.I)
COMM_TERMS   = re.compile(r"ghosted|no follow.?up|never called|can.?t reach|no response|abandoned|left me|nobody called", re.I)


def _source_score(source_name: str) -> int:
    return cfg.SOURCE_AUTHORITY.get(source_name.lower(), cfg.SOURCE_AUTHORITY.get("blog", 12))


def _engagement_score(count) -> int:
    if not count:
        return 1
    count = int(count)
    for threshold, score in cfg.ENGAGEMENT_THRESHOLDS:
        if count >= threshold:
            return score
    return 1


def _recency_mult(published_at) -> float:
    if not published_at:
        return cfg.RECENCY_MULTIPLIER["this_week"]
    if isinstance(published_at, str):
        try:
            published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except Exception:
            return cfg.RECENCY_MULTIPLIER["this_week"]
    now  = datetime.now(published_at.tzinfo) if published_at.tzinfo else datetime.now()
    days = (now - published_at).days
    if days <= 1:  return cfg.RECENCY_MULTIPLIER["today"]
    if days <= 7:  return cfg.RECENCY_MULTIPLIER["this_week"]
    if days <= 30: return cfg.RECENCY_MULTIPLIER["this_month"]
    return cfg.RECENCY_MULTIPLIER["older"]


def calculate_score(source_name, sentiment, engagement_count=None, published_at=None,
                    title="", snippet="", related_surgeon=None, related_location=None,
                    related_procedure=None, narrative_type=None):

    text = f"{title} {snippet or ''}"

    source_score   = _source_score(source_name)
    sentiment_mult = cfg.SENTIMENT_MULTIPLIER.get(sentiment, 1.0)
    eng_score      = _engagement_score(engagement_count)
    recency_mult   = _recency_mult(published_at)

    base = (source_score + eng_score) * sentiment_mult * recency_mult

    bonuses = {}

    # Medical / legal severity
    if LEGAL_TERMS.search(text):
        bonuses["legal_language"]  = cfg.RISK_BONUSES["legal_language"]
    if SAFETY_TERMS.search(text):
        bonuses["patient_safety"]  = cfg.RISK_BONUSES["patient_safety"]
    if REGULATORY.search(text):
        bonuses["regulatory_terms"]= cfg.RISK_BONUSES["regulatory_terms"]

    # Plastic surgery narrative bonuses
    if BOTCHED_TERMS.search(text) or narrative_type == "botched_allegation":
        bonuses["botched_allegation"] = cfg.RISK_BONUSES["botched_allegation"]
    if PAYMENT_TERMS.search(text) or narrative_type == "payment_dispute":
        bonuses["narrative_payment"]  = cfg.RISK_BONUSES["narrative_payment"]
    if COMM_TERMS.search(text) or narrative_type == "communication_failure":
        bonuses["narrative_communication"] = cfg.RISK_BONUSES["narrative_communication"]

    # TikTok amplification — aesthetic fear narratives spread fastest here
    if source_name.lower() == "tiktok":
        bonuses["tiktok_source"]   = cfg.RISK_BONUSES["tiktok_source"]

    # Viral signals
    if VIRAL_TERMS.search(text) or (engagement_count and int(engagement_count) > 50000):
        bonuses["viral_indicators"] = cfg.RISK_BONUSES["viral_indicators"]

    # Entity specificity increases stakes
    if related_surgeon:   bonuses["surgeon_named"]   = cfg.RISK_BONUSES["surgeon_named"]
    if related_location:  bonuses["location_named"]  = cfg.RISK_BONUSES["location_named"]
    if related_procedure: bonuses["procedure_named"] = cfg.RISK_BONUSES["procedure_named"]

    bonus_total  = sum(bonuses.values())
    impact_score = max(1, min(100, round(base + bonus_total)))

    # Risk level
    for level, (lo, hi) in cfg.RISK_LEVELS.items():
        if lo <= impact_score <= hi:
            risk_level = level
            break
    else:
        risk_level = "low"

    score_factors = {
        "source_authority":    round(source_score),
        "sentiment_mult":      sentiment_mult,
        "engagement_score":    round(eng_score),
        "recency_mult":        round(recency_mult, 2),
        "base_score":          round(base),
        **bonuses,
    }

    t = cfg.ALERT_THRESHOLDS
    notify_leadership = impact_score > t["notify_leadership_above"]
    needs_legal       = impact_score > t["needs_legal_above"] or bool(
        bonuses.get("legal_language") or bonuses.get("regulatory_terms") or bonuses.get("botched_allegation")
    )
    is_threat         = impact_score > t["flag_threat_above"] and sentiment == "negative"
    is_opportunity    = sentiment == "positive" and impact_score <= 80

    return {
        "impact_score":      impact_score,
        "risk_level":        risk_level,
        "score_factors":     score_factors,
        "notify_leadership": notify_leadership,
        "needs_legal_review":needs_legal,
        "is_threat":         is_threat,
        "is_opportunity":    is_opportunity,
    }


def risk_color(level):
    return {"low": "#22c55e", "moderate": "#f59e0b", "high": "#f97316", "critical": "#ef4444"}.get(level, "#64748b")


def sentiment_color(s):
    return {"positive": "#22c55e", "negative": "#ef4444", "neutral": "#64748b"}.get(s, "#64748b")


NARRATIVE_LABELS = {
    "botched_allegation":    "⚠ Botched Allegation",
    "safety_complaint":      "🚨 Safety Concern",
    "legal_threat":          "⚖ Legal Threat",
    "payment_dispute":       "💳 Payment Dispute",
    "staff_complaint":       "😠 Staff Complaint",
    "scheduling_complaint":  "📅 Scheduling",
    "recovery_concern":      "🩹 Recovery Concern",
    "communication_failure": "📵 No Follow-Up",
    "positive_review":       "⭐ Positive Review",
    "general_mention":       "◎ General Mention",
}
