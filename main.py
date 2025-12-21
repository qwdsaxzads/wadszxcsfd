import json
import os
import time
from typing import Any, Dict, List, Optional

import feedparser
import requests
from bs4 import BeautifulSoup

# --- CONFIGURATION (Loaded from GitHub Secrets) ---
SUBREDDIT = os.getenv("SUBREDDIT", "").strip()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

TOP_TIME = os.getenv("TOP_TIME", "day").strip()
STATE_FILE = "state.json"
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "30"))
EMBEDS_PER_MESSAGE = 10
EMBED_COLOR_RED = 0xFF0000
USER_AGENT = "github-actions-reddit-rss/1.0"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
BLOCKLIST_TERMS = {"loli", "lolicon", "shota", "shotacon", "underage", "minor", "kid", "child", "middle school", "elementary"}

def load_state() -> Dict[str, List[str]]:
    if not os.path.exists(STATE_FILE):
        return {"new": [], "hot": [], "top": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k in ("new", "hot", "top"):
            if k not in d or not isinstance(d[k], list):
                d[k] = []
        return d
    except Exception:
        return {"new": [], "hot": [], "top": []}

def save_state(state: Dict[str, List[str]]) -> None:
    for k in ("new", "hot", "top"):
        state[k] = state[k][-4000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fetch_rss(url: str) -> List[Any]:
    feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
    return getattr(feed, "entries", []) or []

def entry_uid(entry: Any) -> str:
    return getattr(entry, "id", None) or getattr(entry, "link", None) or getattr(entry, "title", "unknown")

def entry_title(entry: Any) -> str:
    return (getattr(entry, "title", "") or "").strip()

def title_blocked(title: str) -> bool:
    t = title.lower()
    return any(term in t for term in BLOCKLIST_TERMS)

def normalize_url(u: str) -> str:
    return u.replace("&amp;", "&")

def extract_urls_from_html(html: str) -> List[str]:
    out: List[str] = []
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            out.append(src)
    for a in soup.find_all("a"):
        href = a.get("href")
        if href:
            out.append(href)
    return out

def guess_ext(url: str) -> str:
    u = url.split("?", 1)[0].split("#", 1)[0]
    return os.path.splitext(u)[1].lower()

def pick_media_url(entry: Any) -> Optional[str]:
    candidates: List[str] = []
    media = getattr(entry, "media_content", None)
    if media and isinstance(media, list):
        for m in media:
            u = m.get("url")
            if u:
                candidates.append(u)
    content = getattr(entry, "content", None)
    if content and isinstance(content, list) and content:
        candidates += extract_urls_from_html(content[0].get("value", ""))
    summary = getattr(entry, "summary", None)
    if summary:
        candidates += extract_urls_from_html(summary)
    seen = set()
    cleaned: List[str] = []
    for u in candidates:
        u2 = normalize_url(u)
        if u2 not in seen:
            seen.add(u2)
            cleaned.append(u2)
    for u in cleaned:
        if guess_ext(u) in IMAGE_EXTS:
            return u
    return None

def discord_post_embeds(embeds: List[Dict[str, Any]]) -> None:
    payload = {"content": "", "embeds": embeds, "allowed_mentions": {"parse": []}}
    while True:
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
            if r.status_code == 429:
                try:
                    wait_s = float(r.json().get("retry_after", 1.0))
                except Exception:
                    wait_s = 1.0
                time.sleep(wait_s)
                continue
            r.raise_for_status()
            return
        except Exception as e:
            print(f"Error posting to Discord: {e}")
            return

def make_image_only_embed(url: str) -> Dict[str, Any]:
    return {"color": EMBED_COLOR_RED, "image": {"url": url}}

def main() -> None:
    if not WEBHOOK_URL.startswith("https://discord.com/api/webhooks/"):
        print("ERROR: DISCORD_WEBHOOK_URL secret is missing or invalid.")
        return
    if not SUBREDDIT:
        print("ERROR: SUBREDDIT secret is missing.")
        return
    state = load_state()
    feeds = [
        ("new", f"https://old.reddit.com/r/{SUBREDDIT}/new/.rss"),
        ("hot", f"https://old.reddit.com/r/{SUBREDDIT}/hot/.rss"),
        ("top", f"https://old.reddit.com/r/{SUBREDDIT}/top/.rss?t={TOP_TIME}"),
    ]
    merged: Dict[str, str] = {}
    for bucket, url in feeds:
        try:
            print(f"Fetching {bucket}...")
            entries = fetch_rss(url)
        except Exception as e:
            print(f"Failed to fetch {bucket}: {e}")
            continue
        if bucket == "new":
            entries = list(entries)[::-1]
        for e in entries:
            uid = entry_uid(e)
            if uid in list(state[bucket]):
                continue
            state[bucket].append(uid)
            title = entry_title(e)
            if title and title_blocked(title):
                print(f"Skipping blocked title: {title}")
                continue
            media = pick_media_url(e)
            if media and uid not in merged:
                merged[uid] = media
    items = list(merged.items())[:MAX_PER_RUN]
    embeds: List[Dict[str, Any]] = []
    sent = 0
    print(f"Found {len(items)} new images to post.")
    for _, media_url in items:
        embeds.append(make_image_only_embed(media_url))
        if len(embeds) >= EMBEDS_PER_MESSAGE:
            discord_post_embeds(embeds)
            sent += len(embeds)
            embeds = []
            time.sleep(1)
    if embeds:
        discord_post_embeds(embeds)
        sent += len(embeds)
    save_state(state)
    print(f"Success! Sent {sent} item(s).")

if __name__ == "__main__":
    main()
