#!/usr/bin/env python3
"""
Domus Vera Blog — Article Generator Script
===========================================
1. Fetches sector news via Bing News RSS
2. Prints top stories as JSON (for cron agent consumption)
3. Can also directly generate and post an article to the blog API
"""

import json
import sys
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
import html
import re
from datetime import datetime, timezone, timedelta
import os

# ── Configuration ─────────────────────────────────────────
BLOG_API = "https://www.domusvera.it/blog/api.php"
BLOG_ADMIN_USER = "admin"
BLOG_ADMIN_PASS = "DomusVera2026!"
STATE_FILE = "/opt/data/strategie/blog_state.json"

# Bing News RSS queries for Domus Vera sector (simple queries, one topic each)
QUERIES = [
    "affitti brevi",
    "cedolare secca affitti",
    "Airbnb Italia regole",
    "codice CIN locazioni",
    "turismo Italia 2026",
    "B&B ospitalità",
    "locazioni turistiche normative",
    "strutture extralberghiere",
    "host affitti brevi fisco",
    "mercato affitti brevi",
]


def fetch_news(query: str, days: int = 3) -> list[dict]:
    """Fetch news from Bing News RSS for a given query."""
    encoded = urllib.parse.quote(query)
    interval = min(days, 7)
    url = (
        f"https://www.bing.com/news/search?"
        f"q={encoded}&qft=interval%3d%22{interval}%22&format=rss&setlang=it"
    )
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; DomusVeraBlog/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        articles = []
        for item in root.findall('.//item'):
            title = item.findtext('title', '')
            description = item.findtext('description', '')
            link = item.findtext('link', '')
            pub_date_str = item.findtext('pubDate', '')
            
            # Clean HTML entities and tags
            title = html.unescape(title)
            description = html.unescape(re.sub(r'<[^>]+>', '', description))
            
            # Extract real URL from Bing's click wrapper
            if link and 'url=' in link:
                from urllib.parse import parse_qs, urlparse
                parsed = urlparse(link)
                qs = parse_qs(parsed.query)
                if 'url' in qs:
                    link = qs['url'][0]
            
            # Parse date
            pub_date = None
            if pub_date_str:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_date = parsedate_to_datetime(pub_date_str)
                except Exception:
                    pass
            articles.append(
                {
                    "title": title,
                    "description": description[:400],
                    "url": link,
                    "pub_date": pub_date.isoformat() if pub_date else None,
                    "query": query,
                }
            )
        return articles
    except Exception as e:
        print(f"[WARN] Query '{query[:50]}...' failed: {e}", file=sys.stderr)
        return []


def deduplicate(articles: list[dict]) -> list[dict]:
    """Deduplicate by title similarity, keep first occurrence."""
    seen_titles = set()
    result = []
    for a in articles:
        # Normalize title for dedup
        norm = re.sub(r"\s+", " ", a["title"].lower().strip())[:100]
        if norm not in seen_titles:
            seen_titles.add(norm)
            result.append(a)
    return result


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"posted_slugs": [], "last_run": None}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def post_article(article_data: dict) -> dict:
    """Post an article to the blog API. Returns API response."""
    # Login first
    login_data = json.dumps(
        {"username": BLOG_ADMIN_USER, "password": BLOG_ADMIN_PASS}
    ).encode()
    cj = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(cj)

    req = urllib.request.Request(
        f"{BLOG_API}?action=login",
        data=login_data,
        headers={"Content-Type": "application/json"},
    )
    resp = opener.open(req)
    login_result = json.loads(resp.read())
    if not login_result.get("ok"):
        raise RuntimeError(f"Login failed: {login_result}")

    # Create article
    data = json.dumps(article_data).encode()
    req = urllib.request.Request(
        f"{BLOG_API}?action=create_article",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    resp = opener.open(req)
    result = json.loads(resp.read())
    return result


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "fetch"

    if mode == "fetch":
        # Fetch and print news as JSON
        print("[DomusVera Blog] Fetching sector news...", file=sys.stderr)
        all_articles = []
        for i, query in enumerate(QUERIES):
            print(f"  [{i+1}/{len(QUERIES)}] {query[:60]}...", file=sys.stderr)
            articles = fetch_news(query, days=3)
            all_articles.extend(articles)

        all_articles = deduplicate(all_articles)
        # Sort by date (most recent first)
        all_articles.sort(
            key=lambda a: a.get("pub_date") or "1970-01-01", reverse=True
        )

        print(f"[DomusVera Blog] Found {len(all_articles)} unique articles", file=sys.stderr)

        # Filter to last 48h
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        recent = [a for a in all_articles if (a.get("pub_date") or "") >= cutoff]
        if not recent:
            recent = all_articles[:20]  # Fallback to top 20

        print(f"[DomusVera Blog] {len(recent)} from last 48h", file=sys.stderr)
        print(json.dumps(recent, indent=2, ensure_ascii=False))

    elif mode == "post":
        # Read article JSON from stdin
        article = json.load(sys.stdin)
        result = post_article(article)
        print(json.dumps(result, indent=2, ensure_ascii=False))

        # Update state
        if result.get("ok"):
            state = load_state()
            state["posted_slugs"].append(result["article"]["slug"])
            state["last_run"] = datetime.now(timezone.utc).isoformat()
            # Keep only last 200
            state["posted_slugs"] = state["posted_slugs"][-200:]
            save_state(state)

    elif mode == "state":
        state = load_state()
        print(json.dumps(state, indent=2))

    else:
        print(f"Usage: {sys.argv[0]} [fetch|post|state]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
