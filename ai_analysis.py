"""
AI Analysis Service
Uses Anthropic Claude if ANTHROPIC_API_KEY is set in .env; falls back to rule-based mock.
"""

import re
import os
import json

POSITIVE = re.compile(r"amazing|great|excellent|love|best|incredible|recommend|happy|satisfied|professional|beautiful|perfect|wonderful|outstanding|fantastic|transformed|thrilled|results", re.I)
NEGATIVE = re.compile(r"terrible|awful|horrible|lawsuit|complaint|botched|infection|died|death|malpractice|negligence|scam|fraud|unprofessional|refused|disaster|nightmare|dangerous|warning|avoid|regret|ruined|hospitali[zs]", re.I)


def _mock_sentiment(text):
    pos = len(POSITIVE.findall(text))
    neg = len(NEGATIVE.findall(text))
    if neg > pos:
        return "negative"
    if pos > neg:
        return "positive"
    return "neutral"


def _mock_analysis(title, snippet):
    text = f"{title} {snippet or ''}"
    sentiment = _mock_sentiment(text)

    summaries = {
        "positive": "Positive mention of Goals Plastic Surgery highlighting favorable patient experiences or outcomes.",
        "neutral":  "Neutral reference to Goals Plastic Surgery. Content is informational or mixed without strong signal.",
        "negative": "Negative mention flagging potential reputational, legal, or patient safety concerns.",
    }
    actions = {
        "positive": "Monitor for engagement. Consider flagging as a PR opportunity for amplification or repurposing.",
        "neutral":  "No immediate action required. Continue monitoring for tone shifts.",
        "negative": "Assign to PR team for review. Assess legal sensitivity before any public response.",
    }
    return {
        "sentiment": sentiment,
        "ai_summary": summaries[sentiment],
        "why_it_matters": "Auto-analyzed via rule-based model. Add ANTHROPIC_API_KEY to .env for full AI-powered insight.",
        "recommended_action": actions[sentiment],
        "public_response": sentiment == "negative",
    }


def analyze_mention(title, snippet):
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return _mock_analysis(title, snippet)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = f"""You are a PR intelligence analyst for Goals Plastic Surgery, a multi-location plastic surgery company.

Analyze this online mention and return a JSON object with these exact fields:
- sentiment: "positive" | "neutral" | "negative"
- ai_summary: 1-2 sentence factual summary of the mention
- why_it_matters: 1-2 sentences on the PR implications for Goals Plastic Surgery
- recommended_action: 1-2 sentences on what the PR/legal team should do next
- public_response: true | false — should Goals publicly respond?

Treat legal claims, patient safety allegations, and regulatory language with serious weight.
Consider the healthcare context — reputational risk in plastic surgery is high-stakes.

Mention:
Title: {title}
Content: {snippet or '(no additional content)'}

Return ONLY valid JSON. No markdown fences. No extra text."""

        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        return json.loads(text)
    except Exception as e:
        print(f"[AI] Falling back to mock: {e}")
        return _mock_analysis(title, snippet)
