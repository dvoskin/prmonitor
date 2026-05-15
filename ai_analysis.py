"""
Goals Plastic Surgery — AI Analysis Engine
Classifies online mentions using plastic surgery reputation intelligence.
Uses Claude Haiku for speed; falls back to rule-based analysis if unavailable.
"""

import re
import os
import json

# ── Rule-based classifiers ────────────────────────────────────────────────────

POSITIVE_PAT = re.compile(
    r"amazing|love|beautiful|obsessed|exceeded|recommend|best decision|transformed|"
    r"incredible|happy|satisfied|professional|stunning|natural|perfect|life.?changing|"
    r"worth it|exceeded expectations|dr\. voskin|thank you|gentle|caring",
    re.I
)
NEGATIVE_PAT = re.compile(
    r"terrible|awful|botched|nightmare|infection|died|death|malpractice|negligence|"
    r"lawsuit|sue\b|suing|attorney|lawyer|scarred|disfigured|asymmetric|lopsided|"
    r"refused refund|scam|fraud|cash grab|unprofessional|rude|cancelled|ghosted|"
    r"no follow.?up|left me|abandoned|ripped off|regret|worst|danger|unsafe|"
    r"hospitali[zs]|complication|necrosis|sepsis|icu|warning|avoid|stay away",
    re.I
)

NARRATIVE_RULES = [
    ("botched_allegation",    re.compile(r"botched|disfigured|asymmetric|lopsided|necrosis|scarred|destroyed|ruined my", re.I)),
    ("safety_complaint",      re.compile(r"infection|sepsis|icu|hospitali[zs]|complication|died|death|unsafe|emergency|bleeding", re.I)),
    ("legal_threat",          re.compile(r"lawsuit|attorney|lawyer|malpractice|sue\b|suing|court|settlement|filing|negligence", re.I)),
    ("payment_dispute",       re.compile(r"refund|cash grab|overcharged|billing|financing|payment plan|they took my money|scam|fraud", re.I)),
    ("staff_complaint",       re.compile(r"rude|unprofessional|mean|disrespectful|dismissive|ignored|attitude|staff|receptionist|nurse", re.I)),
    ("scheduling_complaint",  re.compile(r"cancelled|rescheduled|no show|last minute|waited|delayed|wasted my time|appointment", re.I)),
    ("recovery_concern",      re.compile(r"recovery|post.?op|healing|pain|swelling|bruising|results not|too long|not healing", re.I)),
    ("communication_failure", re.compile(r"no follow.?up|ghosted|never called|couldn.t reach|no response|abandoned|left me alone", re.I)),
    ("positive_review",       re.compile(r"recommend|love|amazing|obsessed|incredible|best decision|5 star|exceeded|transformed", re.I)),
]


def _mock_sentiment(text: str) -> str:
    neg = len(NEGATIVE_PAT.findall(text))
    pos = len(POSITIVE_PAT.findall(text))
    if neg > pos: return "negative"
    if pos > neg: return "positive"
    return "neutral"


def _mock_narrative(text: str) -> str:
    for name, pattern in NARRATIVE_RULES:
        if pattern.search(text):
            return name
    return "general_mention"


def _mock_analysis(title: str, snippet: str) -> dict:
    text      = f"{title} {snippet or ''}"
    sentiment = _mock_sentiment(text)
    narrative = _mock_narrative(text)

    summaries = {
        "botched_allegation":    "Patient alleging unsatisfactory surgical outcome or physical complications from a procedure at Goals.",
        "safety_complaint":      "Patient reporting a medical complication or safety concern following a procedure at Goals.",
        "legal_threat":          "Post contains legal language suggesting the author is considering or pursuing legal action against Goals.",
        "payment_dispute":       "Patient expressing frustration over billing, refunds, financing, or perceived financial misconduct.",
        "staff_complaint":       "Negative experience attributed to staff conduct — rudeness, dismissiveness, or unprofessional behavior.",
        "scheduling_complaint":  "Patient complaint about appointment cancellations, delays, or scheduling failures.",
        "recovery_concern":      "Patient expressing concern or dissatisfaction with their recovery progress or post-op care.",
        "communication_failure": "Patient reporting lack of post-operative follow-up, unanswered calls, or feeling abandoned.",
        "positive_review":       "Patient sharing a positive experience or outcome from a procedure at Goals.",
        "general_mention":       "General reference to Goals Plastic Surgery without a strong positive or negative signal.",
    }
    actions = {
        "botched_allegation":    "Escalate to medical director and PR lead immediately. Do NOT respond publicly without legal clearance. Initiate internal case review.",
        "safety_complaint":      "Escalate to medical director. Determine if patient requires follow-up care. Prepare holding statement for legal review.",
        "legal_threat":          "Forward to legal counsel immediately. Do not engage publicly. Document post for records.",
        "payment_dispute":       "Route to patient services for urgent resolution. Offer private communication channel. Prevent public escalation.",
        "staff_complaint":       "Share with location manager for internal review. Consider private outreach to resolve. Document for HR.",
        "scheduling_complaint":  "Route to operations team. Consider private apology and remedy. Monitor for further escalation.",
        "recovery_concern":      "Flag for post-op care team. Patient may need reassurance call. High retention risk.",
        "communication_failure": "Assign to patient coordinator for immediate outreach. High churn and review risk.",
        "positive_review":       "Amplify where possible. Consider requesting video testimonial. Share with marketing.",
        "general_mention":       "Monitor. No immediate action required.",
    }
    outreach_types = {"recovery_concern", "communication_failure", "botched_allegation", "safety_complaint", "scheduling_complaint"}

    return {
        "sentiment":              sentiment,
        "narrative_type":         narrative,
        "ai_summary":             summaries.get(narrative, summaries["general_mention"]),
        "why_it_matters":         "Rule-based analysis active — add ANTHROPIC_API_KEY for full AI insight.",
        "recommended_action":     actions.get(narrative, actions["general_mention"]),
        "response_draft":         "",
        "patient_outreach_needed": narrative in outreach_types and sentiment == "negative",
        "public_response":        sentiment == "negative" and narrative not in ("legal_threat",),
    }


# Max real AI calls per scan before falling back to mock for the rest
AI_CAP_PER_SCAN   = 40
_RATE_LIMIT_GIVE_UP = 3

_ai_calls_this_scan   = 0
_ai_rate_limit_streak = 0


def reset_scan_counter():
    global _ai_calls_this_scan, _ai_rate_limit_streak
    _ai_calls_this_scan   = 0
    _ai_rate_limit_streak = 0


def analyze_mention(title: str, snippet: str) -> dict:
    global _ai_calls_this_scan, _ai_rate_limit_streak

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or _ai_calls_this_scan >= AI_CAP_PER_SCAN or _ai_rate_limit_streak >= _RATE_LIMIT_GIVE_UP:
        return _mock_analysis(title, snippet)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, max_retries=0)

        prompt = f"""You are the reputation intelligence system for Goals Plastic Surgery — a multi-location aesthetic medicine and plastic surgery organization.

Your job is to analyze online mentions and produce actionable operational intelligence for the Goals PR, medical, and executive teams.

Classify this mention and return a JSON object with EXACTLY these fields:

{{
  "sentiment": "positive" | "neutral" | "negative",
  "narrative_type": one of: "botched_allegation" | "safety_complaint" | "legal_threat" | "payment_dispute" | "staff_complaint" | "scheduling_complaint" | "recovery_concern" | "communication_failure" | "positive_review" | "general_mention",
  "ai_summary": "1-2 sentence factual summary of what this person is saying",
  "why_it_matters": "1-2 sentences on specific PR/operational risk or opportunity for Goals",
  "recommended_action": "Specific next step for the Goals team — who should handle it and how",
  "response_draft": "Suggested public reply or DM (empathetic, de-escalating, professional, no liability admission). Empty string if no response needed.",
  "patient_outreach_needed": true | false,
  "public_response": true | false
}}

Context for accurate classification:
- Goals is a multi-location plastic surgery/aesthetics practice
- Patient complaints in this space carry high emotional intensity and virality risk
- Even low-follower social posts can go viral on TikTok and Reddit
- Google reviews directly affect local SEO and patient acquisition
- Legal language is high-priority even in vague posts
- Recovery-related posts often signal patients who need follow-up care
- "Communication failure" narratives (no follow-up, ghosted) = high churn risk
- Response drafts should invite private communication, avoid admissions, show empathy

Mention to analyze:
Title: {title}
Content: {snippet or "(no additional content)"}

Return ONLY valid JSON. No markdown. No extra text."""

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        result = json.loads(response.content[0].text.strip())
        _ai_calls_this_scan   += 1
        _ai_rate_limit_streak  = 0
        return result

    except Exception as e:
        err = str(e)
        if "429" in err or "rate_limit" in err:
            _ai_rate_limit_streak += 1
            print(f"[AI] Rate limited ({_ai_rate_limit_streak}/{_RATE_LIMIT_GIVE_UP}) — using mock")
        else:
            print(f"[AI] Error, using mock: {e}")
        return _mock_analysis(title, snippet)
