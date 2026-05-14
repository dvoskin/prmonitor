"""
YouTube search connector — completely free, no API key required.
Extracts video results from YouTube's search page JSON.
"""

import requests
import re
import json
import urllib.parse

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def _extract_text(obj) -> str:
    """Recursively extract text from YouTube's {'runs': [{'text': '...'}]} structure."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        if "simpleText" in obj:
            return obj["simpleText"]
        if "runs" in obj:
            return "".join(r.get("text", "") for r in obj["runs"])
    return ""


def search_youtube(query: str) -> list:
    """
    Search YouTube for a query. Returns list of mention dicts.
    Free — parses the ytInitialData JSON blob embedded in YouTube search pages.
    """
    # Wrap in quotes for exact phrase matching
    encoded = urllib.parse.quote(f'"{query}"')
    url = f"https://www.youtube.com/results?search_query={encoded}&sp=CAI%3D"  # sorted by upload date

    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if not r.ok:
            return []

        # Extract ytInitialData JSON
        match = re.search(r"var ytInitialData = (\{.+?\});</script>", r.text)
        if not match:
            match = re.search(r"ytInitialData\s*=\s*(\{.+?\});\s*(?:window|var)", r.text, re.DOTALL)
        if not match:
            return []

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []

        results = []

        # Navigate to video renderers
        contents = (
            data.get("contents", {})
            .get("twoColumnSearchResultsRenderer", {})
            .get("primaryContents", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
        )

        for section in contents:
            items = (
                section.get("itemSectionRenderer", {}).get("contents", [])
            )
            for item in items:
                vr = item.get("videoRenderer")
                if not vr:
                    continue

                video_id   = vr.get("videoId", "")
                title      = _extract_text(vr.get("title", {}))
                channel    = _extract_text(vr.get("ownerText", {}))
                desc_snip  = _extract_text(vr.get("descriptionSnippet", {}))
                view_text  = _extract_text(vr.get("viewCountText", {}))
                pub_text   = _extract_text(vr.get("publishedTimeText", {}))

                # Extract view count as integer
                view_count = None
                vc_match = re.search(r"([\d,]+)", view_text or "")
                if vc_match:
                    try:
                        view_count = int(vc_match.group(1).replace(",", ""))
                    except ValueError:
                        pass

                if not video_id or not title:
                    continue

                snippet = desc_snip or f"YouTube video by {channel}"
                if pub_text:
                    snippet = f"Published: {pub_text}. {snippet}"

                results.append({
                    "title":            title,
                    "url":              f"https://www.youtube.com/watch?v={video_id}",
                    "snippet":          snippet,
                    "author":           channel,
                    "engagement_count": view_count,
                    "engagement_label": "views",
                    "source_name":      "youtube",
                    "platform":         "YouTube",
                    "matched_keyword":  query,
                })

        return results

    except Exception as e:
        print(f"[YouTube] Error for {query!r}: {e}")
        return []
