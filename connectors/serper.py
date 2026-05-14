"""Serper.dev connector — Google Web + Google News search."""

import os
import requests


def search_google(query: str) -> list:
    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key:
        return []
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 10},
            timeout=10,
        )
        data = r.json()
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "source_name": "google",
                "platform": "Google Search",
                "matched_keyword": query,
            }
            for item in data.get("organic", [])
        ]
    except Exception as e:
        print(f"[Serper] Google search error: {e}")
        return []


def search_google_news(query: str) -> list:
    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key:
        return []
    try:
        r = requests.post(
            "https://google.serper.dev/news",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 10},
            timeout=10,
        )
        data = r.json()
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "published_at": item.get("date"),
                "source_name": "google_news",
                "platform": "Google News",
                "matched_keyword": query,
            }
            for item in data.get("news", [])
        ]
    except Exception as e:
        print(f"[Serper] News search error: {e}")
        return []
