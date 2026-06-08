# twitter_web.py — Twitter activity + website content scoring

import re
import time
import requests
from urllib.parse import urlparse

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ─── Twitter / X scoring ──────────────────────────────────────────────────────

def score_twitter(twitter_handle: str | None) -> dict:
    """
    Score a Twitter/X account for legitimacy and activity.
    Returns: {"score": 0-10, "reason": str, "data": dict}
    Uses DuckDuckGo search — no API key, no account needed.
    Nitter public instances are dead in 2026 so we skip them entirely.
    Max 10pts: 10 for confirmed on both indexes, 6 for one hit.
    """
    if not twitter_handle:
        return {"score": 0, "reason": "No Twitter linked", "data": {}}

    handle = twitter_handle.strip()

    if "x.com/" in handle:
        handle = handle.rstrip("/").split("x.com/")[-1]
    elif "twitter.com/" in handle:
        handle = handle.rstrip("/").split("twitter.com/")[-1]

    handle = handle.lstrip("@").split("?")[0].split("/")[0]

    if not handle:
        return {"score": 0, "reason": "Empty handle", "data": {}}

    queries = [
        f"site:twitter.com/{handle}",
        f"site:x.com/{handle}",
    ]

    hits = 0
    for q in queries:
        try:
            r = requests.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": q, "kl": "us-en"},
                headers=HEADERS,
                timeout=8
            )
            if r.status_code == 200 and handle.lower() in r.text.lower():
                hits += 1
        except Exception:
            pass
        time.sleep(0.3)

    if hits >= 2:
        return {
            "score": 10,
            "reason": f"Twitter @{handle} confirmed (indexed on X + Twitter)",
            "data": {"handle": handle, "exists": True, "hits": hits}
        }
    elif hits == 1:
        return {
            "score": 6,
            "reason": f"Twitter @{handle} found (1 index hit)",
            "data": {"handle": handle, "exists": True, "hits": hits}
        }
    else:
        return {
            "score": 2,
            "reason": f"Twitter @{handle} not found / unverifiable",
            "data": {"handle": handle, "exists": False, "hits": 0}
        }


# ─── Website scoring ──────────────────────────────────────────────────────────

def score_website(url: str | None) -> dict:
    """
    Score a project website for legitimacy and content quality.
    Returns: {"score": 0-20, "reason": str, "data": dict}
    """
    if not url:
        return {"score": 0, "reason": "No website", "data": {}}

    # Normalise
    if not url.startswith("http"):
        url = "https://" + url

    # Reject obvious fake/placeholder links
    bad_domains = ["linktr.ee", "t.me", "discord.gg", "docs.google.com"]
    domain = urlparse(url).netloc.lower()
    if any(b in domain for b in bad_domains):
        return {"score": 2, "reason": f"Linktree/Telegram only ({domain})", "data": {"url": url}}

    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
    except Exception as e:
        return {"score": 0, "reason": f"Website unreachable: {e}", "data": {"url": url}}

    if r.status_code != 200:
        return {"score": 0, "reason": f"Website returned {r.status_code}", "data": {"url": url}}

    html  = r.text.lower()
    score = 0
    notes = []
    data  = {"url": url, "domain": domain}

    # Site loads → baseline
    score += 5
    notes.append("Website live")

    # Meaningful content checks
    content_words = ["tokenomics", "roadmap", "whitepaper", "utility", "mission",
                     "community", "launch", "contract", "solana", "blockchain",
                     "liquidity", "supply", "burn", "staking"]
    hits = sum(1 for w in content_words if w in html)
    data["content_keywords"] = hits
    if hits >= 6:
        score += 8
        notes.append(f"Rich content ({hits} keywords)")
    elif hits >= 3:
        score += 4
        notes.append(f"Some content ({hits} keywords)")
    else:
        notes.append("Thin content")

    # Social links present on site
    social_count = sum(1 for s in ["twitter.com", "x.com", "t.me", "discord"] if s in html)
    if social_count >= 2:
        score += 4
        notes.append(f"{social_count} social links")
    elif social_count == 1:
        score += 2

    # Domain age signal — newer TLDs are riskier
    risky_tlds = [".xyz", ".fun", ".gg", ".io", ".ai"]  # not automatically bad but flag
    safe_tlds  = [".com", ".net", ".org"]
    if any(domain.endswith(t) for t in safe_tlds):
        score += 3
        notes.append("Established TLD")

    # Contract address on website (shows transparency)
    if re.search(r'[1-9A-HJ-NP-Za-km-z]{32,44}', r.text):
        score += 2  # small bonus
        notes.append("Contract on site")

    # Penalise copy-paste template sites (lorem ipsum etc.)
    if "lorem ipsum" in html:
        score = max(0, score - 8)
        notes.append("⚠️ Lorem ipsum detected")

    return {
        "score": min(score, 20),
        "reason": " | ".join(notes),
        "data": data
    }


# ─── Pre-launch Twitter scan ──────────────────────────────────────────────────

def search_pre_launch_twitter(coin_name: str, ticker: str) -> dict:
    """
    Search for coins that had Twitter presence BEFORE contract was deployed.
    This catches the "community first" gems.
    Returns {"pre_existing": bool, "score_bonus": int, "reason": str}
    """
    queries = [
        f"${ticker} launch site:twitter.com",
        f"{coin_name} solana token",
        f"${ticker} memecoin",
    ]

    hits = 0
    for q in queries:
        try:
            # Use DuckDuckGo lite (no JS, no API key)
            r = requests.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": q, "kl": "us-en"},
                headers=HEADERS,
                timeout=8
            )
            if r.status_code == 200:
                results_text = r.text.lower()
                if ticker.lower() in results_text or coin_name.lower() in results_text:
                    hits += 1
        except Exception:
            pass
        time.sleep(0.5)  # gentle rate limit

    if hits >= 2:
        return {
            "pre_existing": True,
            "score_bonus": 15,
            "reason": f"Pre-launch Twitter presence found ({hits}/3 searches hit)"
        }
    elif hits == 1:
        return {
            "pre_existing": True,
            "score_bonus": 7,
            "reason": "Some pre-launch Twitter activity found"
        }
    else:
        return {
            "pre_existing": False,
            "score_bonus": 0,
            "reason": "No pre-launch Twitter presence"
        }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_number(text: str, pattern: str) -> int:
    m = re.search(pattern, text, re.I)
    if not m:
        return 0
    raw = m.group(1).replace(",", "").replace(".", "")
    try:
        return int(raw)
    except ValueError:
        return 0
