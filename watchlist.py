# watchlist.py — Monitor promising coins (score 25-39) for buying opportunity
#
# Coins enter watchlist from main.py when:
#   - Score 25-39 (below MIN_SCORE_TO_BUY threshold)
#   - Age < 60 min at time of scoring
#   - Not already in watchlist
#
# Auto-buy triggers (Option B):
#   - Org score >= 70 AND holders growing steadily → auto-buy
#   - Score naturally reaches 40+ on recheck → auto-buy
#
# Manual buy:
#   - /wbuy TICKER → buy immediately regardless of score
#
# Alert triggers (Telegram):
#   - Org score crosses 50 → alert (improving coin)
#   - Org score crosses 70 → urgent alert + auto-buy
#   - Holders grown 50%+ since added → alert
#   - Score improved 5+ points since added → alert
#
# Removed from watchlist when:
#   - Bought (auto or manual)
#   - Age > 48h
#   - Org score 0 after 6h (likely wash trading)
#   - Liquidity dropping consistently (3 checks in a row)
#   - Permanently blacklisted (whale reject etc)

import time
import json
import os
import requests
import config
import telegram_bot as tg

HEADERS = {"User-Agent": "Mozilla/5.0 MemeSniper/1.0"}

WATCHLIST_FILE = "watchlist.json"

# Monitoring interval — called from main.py
WATCHLIST_CHECK_SEC = 300       # check every 5 minutes

# Thresholds
ORG_SCORE_ALERT     = 30        # send alert when org score crosses this
ORG_SCORE_AUTO_BUY  = 50        # auto-buy when org score crosses this
HOLDER_GROWTH_MIN   = 0.20      # 20% holder growth since added = alert
MAX_AGE_HOURS       = 48        # remove after 48h
ORG_ZERO_HOURS      = 6         # remove if org score still 0 after 6h
MAX_WATCHLIST       = 30        # max coins on watchlist at once

# ─── Persistence ─────────────────────────────────────────────────────────────

def _load_watchlist() -> dict:
    if not os.path.exists(WATCHLIST_FILE):
        return {}
    try:
        with open(WATCHLIST_FILE, "r") as f:
            data = json.load(f)
        if data:
            print(f"[watchlist] Restored {len(data)} coin(s) from disk")
        return data
    except Exception as e:
        print(f"[watchlist] Could not load watchlist: {e}")
        return {}


def _save_watchlist():
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(watchlist, f, indent=2)
    except Exception as e:
        print(f"[watchlist] Could not save: {e}")


# watchlist: { mint: {
#   name, ticker, added_time, initial_score, initial_holders,
#   initial_liq, last_check_time, last_score, last_org_score,
#   last_holders, last_liq, alerted_50, alerted_70,
#   liq_drop_count, age_at_add
# }}
watchlist: dict[str, dict] = _load_watchlist()


# ─── Public API ──────────────────────────────────────────────────────────────

def add_to_watchlist(token: dict, score: int):
    """
    Called from main.py when a coin scores 25-39.
    Adds to watchlist for ongoing monitoring.
    """
    mint = token.get("mint", "")
    if not mint or mint in watchlist:
        return

    # Don't add if already bought via normal flow
    try:
        import monitor as _mon
        import hold_manager as _hm
        if mint in _mon.positions or mint in _hm.hold_positions:
            return
    except Exception:
        pass

    if len(watchlist) >= MAX_WATCHLIST:
        # Remove oldest entry to make room
        oldest = min(watchlist.items(), key=lambda x: x[1]["added_time"])
        watchlist.pop(oldest[0], None)

    watchlist[mint] = {
        "name":           token.get("name", "?"),
        "ticker":         token.get("ticker", "?"),
        "added_time":     time.time(),
        "age_at_add":     token.get("age_minutes", 0),
        "initial_score":  score,
        "initial_holders": token.get("holders", 0),
        "initial_liq":    token.get("liquidity_usd", 0),
        "last_check_time": time.time(),
        "last_score":     score,
        "last_org_score": None,
        "last_holders":   token.get("holders", 0),
        "last_liq":       token.get("liquidity_usd", 0),
        "alerted_50":     False,
        "alerted_70":     False,
        "liq_drop_count": 0,
        "buy_triggered":  False,
    }
    _save_watchlist()
    print(f"[watchlist] Added {token.get('ticker','?')} (score={score})")

    # Alert on new addition — one time only
    tg.send(
        f"👀 <b>Added to Watchlist</b>\n"
        f"Token:   {token.get('name','?')} (${token.get('ticker','?')})\n"
        f"Score:   {score}/100\n"
        f"Age:     {token.get('age_minutes',0):.0f} min\n"
        f"Holders: {token.get('holders',0):,}\n"
        f"Liq:     ${token.get('liquidity_usd',0):,.0f}\n"
        f"\n"
        f"⏳ Monitoring for org score 30+ and holder growth...\n"
        f"🔗 <a href='https://dexscreener.com/solana/{token.get('mint','')}'>DexScreener</a>"
    )


def remove_from_watchlist(mint: str):
    watchlist.pop(mint, None)
    _save_watchlist()


def find_mint_by_ticker(ticker: str) -> str | None:
    """Used by /wbuy command."""
    ticker_lower = ticker.lower().lstrip("$")
    for mint, entry in watchlist.items():
        if entry.get("ticker", "").lower() == ticker_lower:
            return mint
    return None


# ─── Main check loop (called from main.py every 30 min) ─────────────────────

def check_watchlist(bot_state: dict) -> list[str]:
    """
    Check all watchlist coins for buy signals.
    Returns list of mints to auto-buy (caller handles actual buy).
    """
    if not watchlist:
        return []

    to_buy   = []
    to_remove = []
    now = time.time()

    for mint, entry in list(watchlist.items()):
        try:
            # ── Already bought via normal flow — remove silently ─────
            import monitor as _mon
            import hold_manager as _hm
            if mint in _mon.positions or mint in _hm.hold_positions:
                print(f"[watchlist] {entry['ticker']} already in positions — removing from watchlist")
                to_remove.append(mint)
                continue

            age_hours = (now - entry["added_time"]) / 3600

            # ── Expiry checks ────────────────────────────────────────
            if age_hours >= MAX_AGE_HOURS:
                print(f"[watchlist] {entry['ticker']} expired (48h) — removing")
                to_remove.append(mint)
                continue

            # ── Fetch fresh data ─────────────────────────────────────
            dex = _get_dexscreener(mint)
            if not dex:
                continue

            current_liq      = dex.get("liquidity_usd", 0)
            current_holders  = dex.get("holders", entry["last_holders"])
            current_price    = dex.get("price_usd", 0)
            age_minutes      = (now - (entry["added_time"] - entry["age_at_add"] * 60)) / 60

            # Fetch org score
            org_score = _get_org_score(mint)

            # ── Remove conditions ────────────────────────────────────

            # Org score 0 after 6h — wash trading, not worth watching
            if age_hours >= ORG_ZERO_HOURS and org_score is not None and org_score == 0:
                print(f"[watchlist] {entry['ticker']} org score 0 after {age_hours:.1f}h — removing")
                tg.send(
                    f"🗑 <b>Watchlist removed</b>: ${entry['ticker']}\n"
                    f"Reason: Org score 0 after {age_hours:.1f}h — likely wash trading"
                )
                to_remove.append(mint)
                continue

            # Liquidity consistently dropping
            if current_liq < entry["last_liq"] * 0.85:
                entry["liq_drop_count"] = entry.get("liq_drop_count", 0) + 1
            else:
                entry["liq_drop_count"] = 0

            if entry["liq_drop_count"] >= 3:
                print(f"[watchlist] {entry['ticker']} liq dropping 3 checks — removing")
                to_remove.append(mint)
                continue

            # ── Re-score the coin ────────────────────────────────────
            new_score = _quick_rescore(mint, entry, current_liq, current_holders, org_score, age_minutes)

            # ── Already pumped too much — alert only, no auto-buy ────
            # If coin is already up 300%+ in 1h or 500%+ in 24h it's too late to enter safely
            price_change_1h  = dex.get("price_change_1h", 0)
            price_change_24h = dex.get("price_change_24h", 0)
            already_pumped   = price_change_1h > 300 or price_change_24h > 500

            # ── Alert conditions ─────────────────────────────────────

            # Holder growth alert
            initial_holders = entry["initial_holders"] or 1
            holder_growth   = (current_holders - initial_holders) / initial_holders

            # Org score 30+ alert — only if holders also growing 7%+
            if org_score and org_score >= ORG_SCORE_ALERT and not entry["alerted_50"]:
                if current_holders > entry["initial_holders"] * 1.07:
                    entry["alerted_50"] = True
                    _send_watchlist_alert(mint, entry, new_score, org_score,
                                          current_holders, current_liq, holder_growth,
                                          f"🔔 Org score {org_score:.0f} + holders growing {holder_growth*100:.0f}% — gaining legitimacy")

            # Org score 70+ alert + auto-buy check
            if org_score and org_score >= ORG_SCORE_AUTO_BUY and not entry["alerted_70"]:
                entry["alerted_70"] = True
                # Check holder growth is also positive
                holders_growing = current_holders > entry["initial_holders"] * 1.20

                if holders_growing and not bot_state.get("paused") and not already_pumped:
                    print(f"[watchlist] AUTO-BUY triggered: {entry['ticker']} org={org_score:.0f} holders +{holder_growth*100:.0f}%")
                    to_buy.append(mint)
                    entry["buy_triggered"] = True
                    _send_watchlist_alert(mint, entry, new_score, org_score,
                                          current_holders, current_liq, holder_growth,
                                          f"🚀 AUTO-BUY: Org score {org_score:.0f} + holders growing +{holder_growth*100:.0f}%")
                else:
                    reason_str = f"⚡ Org score {org_score:.0f}"
                    if already_pumped:
                        reason_str += f" — already pumped {price_change_1h:.0f}% 1h / {price_change_24h:.0f}% 24h — manual entry only"
                    else:
                        reason_str += f" — use /wbuy {entry['ticker']} to buy"
                    _send_watchlist_alert(mint, entry, new_score, org_score,
                                          current_holders, current_liq, holder_growth,
                                          reason_str)

            # Score naturally improved to 40+ — auto-buy ONLY if safe conditions met
            if new_score >= config.MIN_SCORE_TO_BUY and not entry.get("buy_triggered") and not bot_state.get("paused"):
                # Safety checks — don't buy if holders data is bad or org score is suspicious
                holders_valid   = current_holders > 0 and current_holders >= entry["initial_holders"] * 1.07
                holders_initial = entry.get("initial_holders", 0)

                # Org score logic based on how long we've been watching:
                # - Under 1h: None is ok (too young for Jupiter to score)
                # - Over 1h: None is suspicious (Jupiter should have scored by now)
                # - 0: always block (confirmed inorganic)
                # - 20+: allow (some organic activity confirmed)
                age_hours_watched = (now - entry["added_time"]) / 3600
                if org_score is None:
                    org_ok = age_hours_watched < 1.0   # None only ok under 1h
                elif org_score == 0:
                    org_ok = False                      # confirmed inorganic
                else:
                    org_ok = org_score >= 20            # some organic activity

                if holders_valid and org_ok and holders_initial > 0 and not already_pumped:
                    print(f"[watchlist] AUTO-BUY triggered: {entry['ticker']} score reached {new_score}")
                    to_buy.append(mint)
                    entry["buy_triggered"] = True
                    tg.send(
                        f"✅ <b>Watchlist score reached {new_score}!</b>\n"
                        f"Token: {entry['name']} (${entry['ticker']})\n"
                        f"Was: {entry['initial_score']} → Now: {new_score}\n"
                        f"Org score: {org_score if org_score is not None else 'N/A'}\n"
                        f"Holders: {current_holders:,} (+{holder_growth*100:.0f}%)\n"
                        f"Auto-buying now... 🤖"
                    )
                else:
                    # Not safe to auto-buy — alert instead
                    print(f"[watchlist] Score {new_score} but unsafe to auto-buy (holders={current_holders} org={org_score}) — alerting only")
                    tg.send(
                        f"⚠️ <b>Watchlist score {new_score} but skipping auto-buy</b>\n"
                        f"Token: {entry['name']} (${entry['ticker']})\n"
                        f"Org score: {org_score if org_score is not None else 'N/A'} | Holders: {current_holders:,}\n"
                        f"Use /wbuy {entry['ticker']} to buy manually if you want."
                    )

            # Update entry
            entry["last_check_time"] = now
            entry["last_score"]      = new_score
            entry["last_org_score"]  = org_score
            entry["last_holders"]    = current_holders
            entry["last_liq"]        = current_liq

        except Exception as e:
            print(f"[watchlist] Error checking {entry.get('ticker','?')}: {e}")

    # Clean up
    for mint in to_remove:
        remove_from_watchlist(mint)

    _save_watchlist()
    return to_buy


# ─── Manual buy via /wbuy ────────────────────────────────────────────────────

def trigger_manual_buy(mint: str) -> dict | None:
    """
    Called by /wbuy Telegram command.
    Returns the watchlist entry so main.py can execute the buy,
    or None if not found.
    """
    entry = watchlist.get(mint)
    if not entry:
        return None
    entry["buy_triggered"] = True
    _save_watchlist()
    return entry


# ─── Alert sender ────────────────────────────────────────────────────────────

def _send_watchlist_alert(mint, entry, score, org_score, holders, liq, holder_growth, reason):
    hours_watched = (time.time() - entry["added_time"]) / 3600
    tg.send(
        f"👀 <b>WATCHLIST ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Token:   {entry['name']} (${entry['ticker']})\n"
        f"Reason:  {reason}\n"
        f"\n"
        f"Score:    {entry['initial_score']} → {score}\n"
        f"Org:      {org_score:.0f}/100\n" if org_score else f"Org:      N/A\n"
        f"Holders: {holders:,} (+{holder_growth*100:.0f}% since added)\n"
        f"Liq:     ${liq:,.0f}\n"
        f"Watched: {hours_watched:.1f}h\n"
        f"\n"
        f"🔗 <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a> | "
        f"<a href='https://jup.ag/swap/SOL-{mint}'>Jupiter</a>\n"
        f"Use /wbuy {entry['ticker']} to buy manually"
    )


# ─── Quick rescore ────────────────────────────────────────────────────────────

def _quick_rescore(mint, entry, liq, holders, org_score, age_minutes) -> int:
    """
    Lightweight rescore using current data.
    Not as detailed as scorer.py — just enough to detect improvement.
    Uses same point values as scorer.py for consistency.
    """
    score = entry["last_score"]  # start from last known score

    # Adjust for holder growth
    initial = entry["initial_holders"] or 1
    if holders > initial * 2:
        score += 4   # doubled since added
    elif holders > initial * 1.5:
        score += 2

    # Adjust for org score improvement
    if org_score:
        if org_score >= 50:
            score += 7
        elif org_score >= 25:
            score += 5
        elif org_score >= 10:
            score += 2

    # Liquidity improving
    initial_liq = entry["initial_liq"] or 1
    if liq > initial_liq * 1.5:
        score += 3
    elif liq > initial_liq * 1.2:
        score += 1

    return min(score, 100)


# ─── DexScreener + RugCheck fetcher ─────────────────────────────────────────

def _get_dexscreener(mint: str) -> dict | None:
    try:
        r = requests.get(
            f"{config.DEXSCREENER_URL}/latest/dex/tokens/{mint}",
            headers=HEADERS,
            timeout=8
        )
        r.raise_for_status()
        pairs = r.json().get("pairs", [])
        if not pairs:
            return None
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))

        # Try holders from pairs data first, then info block
        holders = int(best.get("holders", 0) or 0)
        if holders == 0:
            info = best.get("info", {})
            holders = int(info.get("holders", 0) or 0)

        # If still 0 — use RugCheck like scanner does (most reliable source)
        if holders == 0:
            holders = _get_rugcheck_holders(mint)

        pc = best.get("priceChange", {})
        return {
            "price_usd":      float(best.get("priceUsd", 0) or 0),
            "liquidity_usd":  float(best.get("liquidity", {}).get("usd", 0) or 0),
            "holders":        holders,
            "fdv":            float(best.get("fdv", 0) or 0),
            "price_change_1h": float(pc.get("h1", 0) or 0),
            "price_change_6h": float(pc.get("h6", 0) or 0),
            "price_change_24h": float(pc.get("h24", 0) or 0),
        }
    except Exception:
        return None


def _get_rugcheck_holders(mint: str) -> int:
    """Fetch holder count from RugCheck — same source as scanner/scorer."""
    try:
        # Try v1 summary endpoint first
        r = requests.get(
            f"{config.RUGCHECK_URL}/tokens/{mint}/report/summary",
            headers=HEADERS,
            timeout=6
        )
        if r.status_code == 200:
            data = r.json()
            top_h = data.get("topHolders") or []
            holders = data.get("totalHolders", len(top_h) if top_h else 0)
            if holders and holders > 0:
                return int(holders)

        # Fallback to full report
        r2 = requests.get(
            f"{config.RUGCHECK_URL}/tokens/{mint}/report",
            headers=HEADERS,
            timeout=6
        )
        if r2.status_code == 200:
            data2 = r2.json()
            top_h2 = data2.get("topHolders") or []
            holders2 = data2.get("totalHolders", len(top_h2) if top_h2 else 0)
            if holders2 and holders2 > 0:
                return int(holders2)
    except Exception:
        pass
    return 0


# ─── Org score fetcher ────────────────────────────────────────────────────────

def _get_org_score(mint: str) -> float | None:
    """Fetch Jupiter organic score independently."""
    try:
        headers = {"Accept": "application/json"}
        if hasattr(config, "JUPITER_API_KEY") and config.JUPITER_API_KEY:
            headers["x-api-key"] = config.JUPITER_API_KEY
        r = requests.get(
            f"https://api.jup.ag/tokens/v2/{mint}",
            headers=headers,
            timeout=5
        )
        if r.status_code != 200:
            return None
        data = r.json()
        data = data[0] if isinstance(data, list) else data
        raw = data.get("organicScore")
        return float(raw) if raw is not None else None
    except Exception:
        return None


# ─── Summary for /status ─────────────────────────────────────────────────────

def get_watchlist_summary() -> str:
    if not watchlist:
        return "No coins on watchlist."
    lines = [f"👀 Watchlist ({len(watchlist)}/{MAX_WATCHLIST}):"]
    for mint, entry in watchlist.items():
        hours = (time.time() - entry["added_time"]) / 3600
        org   = entry.get("last_org_score")
        org_str = f" org={org:.0f}" if org else ""
        lines.append(
            f"  • {entry['ticker']}: score {entry['initial_score']}→{entry['last_score']}"
            f"{org_str} | "
            f"holders={entry['last_holders']:,} | "
            f"{hours:.1f}h watched"
        )
    return "\n".join(lines)
