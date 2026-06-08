# scanner.py — Scan pump.fun (via pumpportal), Raydium, and DexScreener for new meme coins

import time
import json
import os
import threading
import requests
import config

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# ─── Persistent seen-mints (survives restarts) ────────────────────────────────

SEEN_FILE = "seen_mints.json"

def _load_seen() -> set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
        cutoff = time.time() - 7_200   # 2 hours
        fresh  = {m: t for m, t in data.items() if t > cutoff}
        if len(fresh) != len(data):
            _save_seen_dict(fresh)
        return set(fresh.keys())
    except Exception:
        return set()

def _save_seen_dict(d: dict):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(d, f)
    except Exception as e:
        print(f"[scanner] Could not save seen_mints: {e}")

def _add_seen(mints: list[str]):
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r") as f:
                data = json.load(f)
        else:
            data = {}
        now = time.time()
        for m in mints:
            data[m] = now
            _seen_mints.add(m)   # keep in-memory set in sync
        _save_seen_dict(data)
    except Exception as e:
        print(f"[scanner] Could not update seen_mints: {e}")

_seen_mints: set[str] = _load_seen()
print(f"[scanner] Loaded {len(_seen_mints)} previously seen mints from disk")

def mark_token_seen(mint: str):
    """Call from main.py after a permanent outcome (buy, hard reject, dead coin).
    Do NOT call for holders/volume rejects — those get requeued via _add_pending."""
    if mint and mint not in _seen_mints:
        _seen_mints.add(mint)
        _add_seen([mint])

# ─── Pending queue (coins with $0 liquidity, recheck every 10 min) ───────────
PENDING_FILE        = "pending_mints.json"
_pending: dict      = {}
_pending_lock       = threading.Lock()
PENDING_RECHECK_SEC = 600   # 10 minutes
PENDING_EXPIRE_SEC  = 1800  # drop after 30 mins if still no liquidity

def _load_pending():
    global _pending
    if not os.path.exists(PENDING_FILE):
        return
    try:
        with open(PENDING_FILE, "r") as f:
            data = json.load(f)
        now = time.time()
        _pending = {}
        for m, v in data.items():
            if len(v) == 3:
                first_seen, last_checked, tok = v
            else:
                first_seen, tok = v   # handle old 2-tuple files
                last_checked = first_seen
            if now - float(first_seen) < PENDING_EXPIRE_SEC:
                _pending[m] = (float(first_seen), float(last_checked), tok)
        print(f"[pending] Loaded {len(_pending)} pending coins from disk")
    except Exception as e:
        print(f"[pending] Could not load pending: {e}")

def _save_pending():
    try:
        with _pending_lock:
            data = {m: (ts, lc, tok) for m, (ts, lc, tok) in _pending.items()}
        with open(PENDING_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[pending] Could not save pending: {e}")

def _add_pending(token: dict):
    mint = token.get("mint", "")
    if not mint:
        return
    with _pending_lock:
        if mint not in _pending:
            now = time.time()
            _pending[mint] = (now, now, token)  # (first_seen, last_checked, token)
    _save_pending()

def _get_pending_due() -> list:
    now = time.time()
    due = []
    expired = []
    with _pending_lock:
        for mint, (first_seen, last_checked, token) in list(_pending.items()):
            age_in_queue = now - first_seen

            # Expire if sat in queue too long
            if age_in_queue > PENDING_EXPIRE_SEC:
                expired.append(mint)
                continue

            if now - last_checked >= PENDING_RECHECK_SEC:
                # Check token's actual age — original age_minutes + time spent in queue
                original_age = token.get("age_minutes", 0)
                current_age  = original_age + (age_in_queue / 60)
                if current_age > 120:
                    # Token is now over 120 min old — dead, no point rechecking
                    print(f"[pending] EXPIRE {mint[:8]}... | age now {current_age:.0f} min — too old")
                    expired.append(mint)
                    continue

                due.append(token)
                _pending[mint] = (first_seen, now, token)  # update last_checked

        for m in expired:
            del _pending[m]
    if expired:
        _save_pending()
    return due

def _remove_pending(mint: str):
    with _pending_lock:
        _pending.pop(mint, None)
    _save_pending()

# Load pending queue on startup
_load_pending()


# ─── PumpPortal queue (populated by background websocket thread) ──────────────

_pumpportal_queue: list[str] = []   # list of new mints from pumpportal
_pp_lock = threading.Lock()
_pp_thread_started = False

def _pumpportal_listener():
    """Background thread: connects to pumpportal websocket and queues new mints."""
    import websocket as ws_lib
    def on_message(ws, message):
        try:
            data = json.loads(message)
            mint = data.get("mint") or data.get("tokenAddress") or data.get("address")
            if mint:
                with _pp_lock:
                    if mint not in _seen_mints and mint not in _pumpportal_queue:
                        _pumpportal_queue.append(mint)
        except Exception:
            pass

    def on_error(ws, error):
        print(f"[pumpportal] ws error: {error}")

    def on_close(ws, *args):
        print("[pumpportal] ws closed, reconnecting in 10s...")
        time.sleep(10)
        _start_pumpportal_thread()

    def on_open(ws):
        print("[pumpportal] Connected — listening for new tokens")
        ws.send(json.dumps({"method": "subscribeNewToken"}))

    try:
        wsapp = ws_lib.WebSocketApp(
            "wss://pumpportal.fun/api/data",
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        wsapp.run_forever(ping_interval=30, ping_timeout=10)
    except Exception as e:
        print(f"[pumpportal] Thread error: {e}")
        time.sleep(10)
        _start_pumpportal_thread()

def _start_pumpportal_thread():
    global _pp_thread_started
    try:
        import websocket  # noqa
        t = threading.Thread(target=_pumpportal_listener, daemon=True)
        t.start()
        _pp_thread_started = True
        print("[pumpportal] Websocket thread started")
    except ImportError:
        print("[pumpportal] websocket-client not installed, skipping. Run: pip install websocket-client --break-system-packages")

# Start pumpportal listener on module load
_start_pumpportal_thread()


# ─── Main entry point ─────────────────────────────────────────────────────────

def get_new_tokens() -> list[dict]:
    tokens = []
    tokens += _scan_pumpportal()
    #tokens += _scan_raydium()  # disabled - rate limited
    tokens += _scan_dexscreener()

    # Recheck pending coins every 10 min
    pending_due = _get_pending_due()
    if pending_due:
        print(f"[pending] Rechecking {len(pending_due)} coins with previously $0 liquidity")
        for t in pending_due:
            mint = t.get("mint", "")
            dex     = _get_dexscreener_token(mint)
            details = _get_rugcheck(mint)
            if dex:
                t.update({
                    "liquidity_usd":   dex.get("liquidity_usd", 0),
                    "volume_5m":       dex.get("volume_5m", 0),
                    "volume_1h":       dex.get("volume_1h", 0),
                    "price_usd":       dex.get("price_usd", 0),
                    "buy_txns_5m":     dex.get("buy_txns_5m", 0),
                    "sell_txns_5m":    dex.get("sell_txns_5m", 0),
                    "price_change_5m": dex.get("price_change_5m", 0),
                    "age_minutes":     dex.get("age_minutes", t.get("age_minutes", 15)),
                    "name":            dex.get("name", t.get("name", "")),
                    "ticker":          dex.get("ticker", t.get("ticker", "???")),
                })
            if details:
                t.update({
                    "holders":                  details.get("holders", t.get("holders", 0)),
                    "dev_wallet_pct":           details.get("dev_wallet_pct", t.get("dev_wallet_pct", 100)),
                    "top_holder_pct":           details.get("top_holder_pct", t.get("top_holder_pct", 100)),
                    "is_dev_top_holder":        details.get("is_dev_top_holder", t.get("is_dev_top_holder", False)),
                    "top10_pct":                details.get("top10_pct", t.get("top10_pct", 100)),
                    "mint_authority_revoked":   details.get("mint_authority_revoked", t.get("mint_authority_revoked", False)),
                    "freeze_authority_revoked": details.get("freeze_authority_revoked", t.get("freeze_authority_revoked", False)),
                    "lp_locked_pct":            details.get("lp_locked_pct", t.get("lp_locked_pct", 0)),
                })
            # If rugcheck returned no holders data, try dexscreener pairs as fallback
            if t.get("holders", 0) == 0 and dex:
                try:
                    r = __import__("requests").get(
                        f"{__import__('config').DEXSCREENER_URL}/latest/dex/tokens/{mint}",
                        headers=HEADERS, timeout=8
                    )
                    pairs = r.json().get("pairs", [])
                    if pairs:
                        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                        dex_holders = best.get("info", {}).get("holders", 0) or 0
                        if dex_holders > 0:
                            t["holders"] = dex_holders
                            print(f"[pending] holders from dexscreener fallback: {dex_holders}")
                except Exception:
                    pass
            tokens.append(t)
            time.sleep(0.3)

    fresh     = []
    new_mints = []
    seen_this_scan = set()

    for t in tokens:
        mint = t.get("mint", "")
        if not mint or mint in seen_this_scan:
            continue
        seen_this_scan.add(mint)
        liq = t.get("liquidity_usd", 0)

        if (liq >= 500 or t.get("volume_5m", 0) >= 200 or t.get("volume_1h", 0) >= 300):
            # Don't mark seen here — let scorer in main.py decide the outcome.
            # mark_token_seen() is called from main.py after a buy or permanent hard reject.
            _remove_pending(mint)
            fresh.append(t)
        else:
            # Only queue if there is some signal — liq > 0 OR holders > 0
            if mint not in _seen_mints:
                if t.get("liquidity_usd", 0) > 0 or t.get("holders", 0) > 0:
                    _add_pending(t)
                else:
                    # Completely dead — mark seen and skip forever
                    _seen_mints.add(mint)
                    new_mints.append(mint)

    if new_mints:
        _add_seen(new_mints)

    return fresh



# ─── PumpPortal scanner (drains websocket queue) ─────────────────────────────

def _scan_pumpportal() -> list[dict]:
    """Process mints queued by the pumpportal websocket listener."""
    with _pp_lock:
        mints = _pumpportal_queue[:20]
        del _pumpportal_queue[:20]

    if not mints:
        return []

    print(f"[pumpportal] Processing {len(mints)} new mints from websocket")
    results = []
    for mint in mints:
        if mint in _seen_mints:
            continue
        try:
            details = _get_rugcheck(mint)
            dex     = _get_dexscreener_token(mint)
            socials = _get_dex_socials(mint)   # Fix 1: real socials for pump.fun tokens
            token = {
                "source":      "pump.fun",
                "mint":        mint,
                "name":        dex.get("name", mint[:8]),
                "ticker":      dex.get("ticker", "???"),
                "twitter":     socials.get("twitter"),    # Fix 1
                "website":     socials.get("website"),    # Fix 1
                "telegram":    socials.get("telegram"),   # Fix 1
                "image":       socials.get("image"),      # Fix 1
                "age_minutes": dex.get("age_minutes", 15),
                "bonding_curve_pct": details.get("bonding_curve_pct", 0),  # Fix 2
                "liquidity_usd":   dex.get("liquidity_usd", 0),
                "volume_5m":       dex.get("volume_5m", 0),
                "volume_1h":       dex.get("volume_1h", 0),
                "price_usd":       dex.get("price_usd", 0),
                "buy_txns_5m":     dex.get("buy_txns_5m", 0),
                "sell_txns_5m":    dex.get("sell_txns_5m", 0),
                "price_change_5m": dex.get("price_change_5m", 0),
                "price_change_1h": dex.get("price_change_1h", 0),
                "holders":                   details.get("holders", 0),
                "dev_wallet_pct":            details.get("dev_wallet_pct"),   # None = unknown
                "top_holder_pct":            details.get("top_holder_pct"),   # None = unknown; single largest wallet
                "is_dev_top_holder":         details.get("is_dev_top_holder", False),
                "top10_pct":                 details.get("top10_pct"),         # None = unknown
                "mint_authority_revoked":    details.get("mint_authority_revoked", False),
                "freeze_authority_revoked":  details.get("freeze_authority_revoked", False),
                "lp_locked_pct":             details.get("lp_locked_pct", 0),
            }

            # Pre-filter: skip obvious rugs before adding to pending
            # Only apply when RugCheck actually returned data — don't reject on unknown
            rugcheck_ok = details.get("data_available", False)
            if not rugcheck_ok:
                print(f"[prefilter] {mint[:8]}... rugcheck no data — passing through to scorer")
            if rugcheck_ok:
                dev_is_top  = token.get("is_dev_top_holder", False)
                top_h_pct   = token.get("top_holder_pct")
                top_h_limit = 10 if dev_is_top else 25
                fail_mint   = not token["mint_authority_revoked"]
                fail_freeze = not token["freeze_authority_revoked"]
                fail_top1   = top_h_pct is not None and top_h_pct > top_h_limit
                fail_top10  = token["top10_pct"] is not None and token["top10_pct"] > 80

                # Hard permanent rejects — mint/freeze auth failures are never recoverable
                if fail_mint or fail_freeze:
                    reason = []
                    if fail_mint:   reason.append("mint_auth_not_revoked")
                    if fail_freeze: reason.append("freeze_auth_not_revoked")
                    print(f"[prefilter] PERM_SKIP {mint[:8]}... | {' | '.join(reason)}")
                    _add_seen([mint])
                    time.sleep(0.3)
                    continue

                # Concentration failures — split by severity
                if fail_top1 or fail_top10:
                    reason = []
                    if fail_top1:  reason.append(f"top1={top_h_pct:.1f}%({'dev' if dev_is_top else 'whale'})>{top_h_limit}%")
                    if fail_top10: reason.append(f"top10={token['top10_pct']:.1f}%>80%")

                    top10_val = token.get("top10_pct") or 0

                    # Hopeless — top1 >70% or top10 >85% means unsold/no real distribution
                    if (top_h_pct is not None and top_h_pct > 70) or top10_val > 85:
                        print(f"[prefilter] PERM_SKIP {mint[:8]}... | hopeless {' | '.join(reason)}")
                        _add_seen([mint])
                    else:
                        # Before queuing — check if signals are permanently hopeless
                        # Dev concentration never improves, old tokens never recover
                        dev_pct  = token.get("dev_wallet_pct") or 0
                        tok_age  = token.get("age_minutes", 0)
                        perm_hopeless = (
                            dev_pct > 40                          # dev holding >40% never distributes down
                            or (top10_val > 80 and tok_age > 30) # concentrated + old = dead
                        )
                        if perm_hopeless:
                            reason_str = f"dev={dev_pct:.1f}%" if dev_pct > 40 else f"top10={top10_val:.1f}%+age={tok_age:.0f}min"
                            print(f"[prefilter] PERM_SKIP {mint[:8]}... | hopeless (no recovery) {reason_str}")
                            _add_seen([mint])
                        else:
                            # top1 25-70% AND top10 80-85%, young token — worth a recheck
                            print(f"[prefilter] QUEUE {mint[:8]}... | {' | '.join(reason)}")
                            _add_pending(token)
                            _add_seen([mint])   # prevent websocket re-processing next cycle
                    time.sleep(0.3)
                    continue

            results.append(token)
            time.sleep(0.3)
        except Exception:
            continue
    return results


# ─── Raydium new pools scanner ────────────────────────────────────────────────

def _scan_raydium() -> list[dict]:
    """Fetch newest Raydium SOL pools."""
    try:
        r = requests.get(
            "https://api.raydium.io/v2/main/pairs",
            headers=HEADERS,
            timeout=10
        )
        r.raise_for_status()
        pairs = r.json()
        if not isinstance(pairs, list):
            return []
    except Exception as e:
        print(f"[scanner] Raydium error: {e}")
        return []

    results = []
    skip = ["USDC", "USDT", "SOL", "WSOL", "BTC", "ETH", "JUP", "RAY", "BONK"]
    now_ms = time.time() * 1000

    # Sort by newest, take top 30
    pairs_sorted = sorted(pairs, key=lambda p: p.get("lpMint", ""), reverse=True)[:50]

    for pair in pairs_sorted:
        # Only SOL quote pairs
        if pair.get("quoteMint") != "So11111111111111111111111111111111111111112":
            continue

        mint = pair.get("baseMint", "")
        if not mint or mint in _seen_mints:
            continue

        ticker = pair.get("name", "").split("-")[0].strip()
        if ticker in skip:
            continue

        liq = float(pair.get("liquidity", 0) or 0)
        if liq < 1000:   # skip very low liquidity
            continue

        details = _get_rugcheck(mint)
        dex     = _get_dexscreener_token(mint)

        token = {
            "source":      "raydium",
            "mint":        mint,
            "name":        pair.get("name", "").split("-")[0].strip(),
            "ticker":      ticker,
            "twitter":     None,
            "website":     None,
            "telegram":    None,
            "image":       None,
            "age_minutes": dex.get("age_minutes", 60),
            "bonding_curve_pct": details.get("bonding_curve_pct", 0),  # Fix 2
            "liquidity_usd":   dex.get("liquidity_usd", liq),
            "volume_5m":       dex.get("volume_5m", float(pair.get("volume24h", 0) or 0) / 288),
            "volume_1h":       dex.get("volume_1h", 0),
            "price_usd":       dex.get("price_usd", float(pair.get("price", 0) or 0)),
            "buy_txns_5m":     dex.get("buy_txns_5m", 0),
            "sell_txns_5m":    dex.get("sell_txns_5m", 0),
            "price_change_5m": dex.get("price_change_5m", 0),
            "holders":                   details.get("holders", 0),
            "dev_wallet_pct":            details.get("dev_wallet_pct", 100),
            "top_holder_pct":            details.get("top_holder_pct", 100),
            "is_dev_top_holder":         details.get("is_dev_top_holder", False),
            "top10_pct":                 details.get("top10_pct", 100),
            "mint_authority_revoked":    details.get("mint_authority_revoked", False),
            "freeze_authority_revoked":  details.get("freeze_authority_revoked", False),
            "lp_locked_pct":             details.get("lp_locked_pct", 0),
        }
        results.append(token)
        time.sleep(0.3)

    return results


# ─── DexScreener scanner ──────────────────────────────────────────────────────

def _scan_dexscreener() -> list[dict]:
    """Fetch latest token profiles from DexScreener."""
    try:
        r = requests.get(
            f"{config.DEXSCREENER_URL}/token-profiles/latest/v1",
            headers=HEADERS,
            timeout=10
        )
        r.raise_for_status()
        profiles = [b for b in r.json() if b.get("chainId") == "solana"]
        mints_to_fetch = [b["tokenAddress"] for b in profiles if b.get("tokenAddress")][:20]
    except Exception as e:
        print(f"[scanner] DexScreener profiles error: {e}")
        return []

    results = []
    skip = ["USDC", "USDT", "SOL", "WSOL", "BTC", "ETH", "JUP", "RAY"]

    for mint in mints_to_fetch:
        if mint in _seen_mints:
            continue
        try:
            rr = requests.get(
                f"{config.DEXSCREENER_URL}/latest/dex/tokens/{mint}",
                headers=HEADERS,
                timeout=10
            )
            rr.raise_for_status()
            pairs = rr.json().get("pairs", [])
            if not pairs:
                continue

            pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            if pair.get("chainId") != "solana":
                continue

            base = pair.get("baseToken", {})
            if base.get("symbol", "") in skip:
                continue

            # Reject if ANY pair is older than 1440 min — relisted rug signal
            all_ages = [_age_minutes_from_iso(p.get("pairCreatedAt", 0)) for p in pairs if p.get("pairCreatedAt")]
            if any(a > 1440 for a in all_ages):
                continue
            age_min = _age_minutes_from_iso(pair.get("pairCreatedAt", 0))

            details = _get_rugcheck(mint)
            info    = pair.get("info", {})

            token = {
                "source":      "dexscreener",
                "mint":        mint,
                "name":        base.get("name", ""),
                "ticker":      base.get("symbol", ""),
                "twitter":     _find_social(info.get("socials", []), "twitter"),
                "website":     info.get("websites", [{}])[0].get("url") if info.get("websites") else None,
                "telegram":    _find_social(info.get("socials", []), "telegram"),
                "image":       info.get("imageUrl"),
                "age_minutes": age_min,
                "bonding_curve_pct": details.get("bonding_curve_pct", 0),  # Fix 2
                "liquidity_usd":   float(pair.get("liquidity", {}).get("usd", 0) or 0),
                "volume_5m":       float(pair.get("volume", {}).get("m5", 0) or 0),
                "volume_1h":       float(pair.get("volume", {}).get("h1", 0) or 0),
                "price_usd":       float(pair.get("priceUsd", 0) or 0),
                "buy_txns_5m":     pair.get("txns", {}).get("m5", {}).get("buys", 0),
                "sell_txns_5m":    pair.get("txns", {}).get("m5", {}).get("sells", 0),
                "price_change_5m": pair.get("priceChange", {}).get("m5", 0),
                "price_change_1h": float(pair.get("priceChange", {}).get("h1", 0) or 0),
                "holders":                   details.get("holders", 0),
                "dev_wallet_pct":            details.get("dev_wallet_pct"),   # None = unknown
                "top_holder_pct":            details.get("top_holder_pct"),   # None = unknown; single largest wallet
                "is_dev_top_holder":         details.get("is_dev_top_holder", False),
                "top10_pct":                 details.get("top10_pct"),         # None = unknown
                "mint_authority_revoked":    details.get("mint_authority_revoked", False),
                "freeze_authority_revoked":  details.get("freeze_authority_revoked", False),
                "lp_locked_pct":             details.get("lp_locked_pct", 0),
            }

            # Same pre-filter as pumpportal — reject obvious rugs early
            rugcheck_ok = details.get("data_available", False)
            if rugcheck_ok:
                dev_is_top  = token.get("is_dev_top_holder", False)
                top_h_pct   = token.get("top_holder_pct")
                top_h_limit = 10 if dev_is_top else 25
                top10_val   = token.get("top10_pct") or 0
                fail_mint   = not token["mint_authority_revoked"]
                fail_freeze = not token["freeze_authority_revoked"]
                fail_top1   = top_h_pct is not None and top_h_pct > top_h_limit
                fail_top10  = top10_val > 80

                if fail_mint or fail_freeze:
                    reason = []
                    if fail_mint:   reason.append("mint_auth_not_revoked")
                    if fail_freeze: reason.append("freeze_auth_not_revoked")
                    print(f"[prefilter/dex] PERM_SKIP {mint[:8]}... | {' | '.join(reason)}")
                    _add_seen([mint])
                    time.sleep(0.3)
                    continue

                if fail_top1 or fail_top10:
                    reason = []
                    if fail_top1:  reason.append(f"top1={top_h_pct:.1f}%({'dev' if dev_is_top else 'whale'})>{top_h_limit}%")
                    if fail_top10: reason.append(f"top10={top10_val:.1f}%>80%")
                    if (top_h_pct is not None and top_h_pct > 70) or top10_val > 85:
                        print(f"[prefilter/dex] PERM_SKIP {mint[:8]}... | hopeless {' | '.join(reason)}")
                        _add_seen([mint])
                    else:
                        # Before queuing — check if signals are permanently hopeless
                        dev_pct = token.get("dev_wallet_pct") or 0
                        tok_age = token.get("age_minutes", 0)
                        perm_hopeless = (
                            dev_pct > 40                          # dev holding >40% never distributes down
                            or (top10_val > 80 and tok_age > 30) # concentrated + old = dead
                        )
                        if perm_hopeless:
                            reason_str = f"dev={dev_pct:.1f}%" if dev_pct > 40 else f"top10={top10_val:.1f}%+age={tok_age:.0f}min"
                            print(f"[prefilter/dex] PERM_SKIP {mint[:8]}... | hopeless (no recovery) {reason_str}")
                            _add_seen([mint])
                        else:
                            print(f"[prefilter/dex] QUEUE {mint[:8]}... | {' | '.join(reason)}")
                            _add_pending(token)
                            _add_seen([mint])   # prevent websocket re-processing next cycle
                    time.sleep(0.3)
                    continue

            results.append(token)
            time.sleep(0.3)
        except Exception:
            continue

    return results


# ─── DexScreener single token ─────────────────────────────────────────────────

def _get_dexscreener_token(mint: str) -> dict:
    try:
        r = requests.get(
            f"{config.DEXSCREENER_URL}/latest/dex/tokens/{mint}",
            headers=HEADERS,
            timeout=8
        )
        r.raise_for_status()
        pairs = r.json().get("pairs", [])
        if not pairs:
            return {}
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        base = best.get("baseToken", {})
        age  = _age_minutes_from_iso(best.get("pairCreatedAt", 0))
        return {
            "name":            base.get("name", ""),
            "ticker":          base.get("symbol", ""),
            "age_minutes":     age,
            "liquidity_usd":   float(best.get("liquidity", {}).get("usd", 0) or 0),
            "volume_5m":       float(best.get("volume", {}).get("m5", 0) or 0),
            "volume_1h":       float(best.get("volume", {}).get("h1", 0) or 0),
            "price_usd":       float(best.get("priceUsd", 0) or 0),
            "buy_txns_5m":     best.get("txns", {}).get("m5", {}).get("buys", 0),
            "sell_txns_5m":    best.get("txns", {}).get("m5", {}).get("sells", 0),
            "price_change_5m": best.get("priceChange", {}).get("m5", 0),
            "price_change_1h": float(best.get("priceChange", {}).get("h1", 0) or 0),
        }
    except Exception:
        return {}


# ─── DexScreener socials ──────────────────────────────────────────────────────

def _get_dex_socials(mint: str) -> dict:
    """Pull twitter/website/telegram from DexScreener pair info for a mint."""
    try:
        r = requests.get(
            f"{config.DEXSCREENER_URL}/latest/dex/tokens/{mint}",
            headers=HEADERS,
            timeout=8
        )
        r.raise_for_status()
        pairs = r.json().get("pairs", [])
        if not pairs:
            return {}
        # Pick pair with highest liquidity
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        info = best.get("info", {})
        socials = info.get("socials", [])
        websites = info.get("websites", [])
        return {
            "twitter":  _find_social(socials, "twitter"),
            "website":  websites[0].get("url") if websites else None,
            "telegram": _find_social(socials, "telegram"),
            "image":    info.get("imageUrl"),
        }
    except Exception:
        return {}


# ─── RugCheck.xyz ─────────────────────────────────────────────────────────────

def _get_rugcheck(mint: str) -> dict:
    data = None

    # Try /report/summary first, then /report, with one retry each (RugCheck is flaky)
    endpoints = [
        f"{config.RUGCHECK_URL}/tokens/{mint}/report",
        f"{config.RUGCHECK_URL}/tokens/{mint}/report/summary",
    ]
    for url in endpoints:
        for attempt in range(2):
            try:
                r = requests.get(url, headers=HEADERS, timeout=10)
                r.raise_for_status()
                candidate = r.json()
                if candidate and ("token" in candidate or "topHolders" in candidate or "markets" in candidate):
                    data = candidate
                    break
            except Exception:
                if attempt == 0:
                    time.sleep(1)
                continue
        if data:
            break

    if not data:
        return {"data_available": False}

    risks          = data.get("risks", [])
    top_h          = data.get("topHolders", [])
    mints          = data.get("token", {})
    markets        = data.get("markets", [{}])
    known_accounts = data.get("knownAccounts", {})
    creator_addr   = data.get("creator", "")

    # Build set of known dev/creator addresses from both creator field and knownAccounts
    dev_addresses = set()
    if creator_addr:
        dev_addresses.add(creator_addr)
    for addr, info in known_accounts.items():
        if info.get("type", "").upper() == "CREATOR":
            dev_addresses.add(addr)

    # Missing RugCheck holder data should not be treated as 0%
    dev_pct = None
    top10_pct = None
    is_dev_top_holder = False

    if top_h:
        # RugCheck sometimes returns pct as a decimal (0.42) and sometimes as
        # a percentage (42.0) — detect which format and normalise to percentage
        raw_pct = float(top_h[0].get("pct", 0))
        multiplier = 1 if raw_pct > 1 else 100
        dev_pct   = raw_pct * multiplier
        top10_pct = sum(float(h.get("pct", 0)) * multiplier for h in top_h[:10])

        # Check if top holder owner matches a known dev/creator address
        top_holder_owner = top_h[0].get("owner", "")
        top_holder_addr  = top_h[0].get("address", "")
        if dev_addresses and (top_holder_owner in dev_addresses or top_holder_addr in dev_addresses):
            is_dev_top_holder = True

    lp_locked = 0.0
    bonding_curve_pct = 0.0
    for m in markets:
        market_type = m.get("marketType", "")
        lp = m.get("lp", {})
        lp_locked_pct = lp.get("lpLockedPct", 0) or m.get("lpLockedPct", 0)

        if market_type == "pump_fun":
            # lpLockedPct=100 on bonding curve is misleading — it's not a real
            # Raydium lock, just the bonding curve contract holding the funds.
            # Leave lp_locked=0 for pre-graduation tokens.

            # Calculate bonding curve progress from SOL in pool vs ~85 SOL target
            lamports = m.get("liquidityBAccount", {}).get("amount", 0)
            sol_in_pool = lamports / 1e9
            bonding_curve_pct = min(round((sol_in_pool / 85) * 100, 1), 100)
        else:
            # Real Raydium/Orca pool — count LP lock normally
            if lp_locked_pct:
                lp_locked = float(lp_locked_pct)
                break

    mint_rev   = not bool(mints.get("mintAuthority"))
    freeze_rev = not bool(mints.get("freezeAuthority"))
    holders    = data.get("totalHolders", len(top_h) if top_h else 0)

    # top_holder_pct = single largest wallet's share (explicit field, separate from dev_wallet_pct)
    top_holder_pct = dev_pct  # dev_pct is already topHolders[0].pct

    return {
        "data_available":           True,
        "holders":                  holders,
        "dev_wallet_pct":           dev_pct,
        "top_holder_pct":           top_holder_pct,
        "is_dev_top_holder":        is_dev_top_holder,
        "top10_pct":                top10_pct,
        "mint_authority_revoked":   mint_rev,
        "freeze_authority_revoked": freeze_rev,
        "lp_locked_pct":            lp_locked,
        "bonding_curve_pct":        bonding_curve_pct,
        "risks":                    risks,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _age_minutes(timestamp_ms: int) -> float:
    if not timestamp_ms:
        return 999
    return (time.time() * 1000 - timestamp_ms) / 60_000

def _age_minutes_from_iso(ts) -> float:
    if not ts:
        return 999
    try:
        return (time.time() * 1000 - int(ts)) / 60_000
    except Exception:
        return 999

def _find_social(socials: list, kind: str) -> str | None:
    for s in socials:
        if s.get("type", "").lower() == kind:
            return s.get("url")
    return None
