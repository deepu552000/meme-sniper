# scorer.py — Token safety scoring (0–100)

import struct
import base64
import requests
import config
from twitter_web import score_twitter, score_website, search_pre_launch_twitter

# ── Pump.fun constants ─────────────────────────────────────────────────────────
_PUMP_PROGRAM_ID       = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_PUMP_CURVE_SEED       = b"bonding-curve"
_BONDING_CURVE_SOL_TARGET = 85.0   # ~85 SOL fills the bonding curve


def _get_bonding_curve_pct(mint: str) -> float:
    """
    Fetch pump.fun bonding curve % filled directly via Alchemy RPC.
    Only called from scorer when RugCheck returned 0 — minimal RPC usage.

    Layout (Anchor 8-byte discriminator + u64 fields):
      offset  0: discriminator        (8 bytes)
      offset  8: virtualTokenReserves (u64)
      offset 16: virtualSolReserves   (u64)
      offset 24: realTokenReserves    (u64)
      offset 32: realSolReserves      (u64)  ← sol actually in curve
      offset 40: tokenTotalSupply     (u64)
      offset 48: complete             (bool)

    realSolReserves / 1e9 / 85 SOL = % filled.
    Returns 0.0 on any failure — scorer falls back to 0 pts, safe default.
    """
    try:
        from solders.pubkey import Pubkey  # type: ignore

        mint_pubkey    = Pubkey.from_string(mint)
        program_pubkey = Pubkey.from_string(_PUMP_PROGRAM_ID)

        pda, _ = Pubkey.find_program_address(
            [_PUMP_CURVE_SEED, bytes(mint_pubkey)],
            program_pubkey
        )

        payload = {
            "jsonrpc": "2.0",
            "id":      1,
            "method":  "getAccountInfo",
            "params":  [
                str(pda),
                {"encoding": "base64", "commitment": "confirmed"}
            ]
        }
        r = requests.post(config.RPC_URL, json=payload, timeout=5)
        r.raise_for_status()
        value = r.json().get("result", {}).get("value")
        if not value:
            return 0.0

        raw = base64.b64decode(value["data"][0])
        if len(raw) < 40:
            return 0.0

        real_sol_reserves = struct.unpack_from("<Q", raw, 32)[0]
        sol_in_curve      = real_sol_reserves / 1e9
        return min(round((sol_in_curve / _BONDING_CURVE_SOL_TARGET) * 100, 1), 100.0)

    except ImportError:
        return 0.0   # solders not installed — skip silently
    except Exception:
        return 0.0


def _get_organic_score(mint: str, age_minutes: float) -> tuple[float | None, int]:
    """
    Fetch Jupiter organic score (0-100) for a token.
    Returns (raw_score_or_None, pts_to_add).

    Skipped entirely for tokens under 25 min — Jupiter has no meaningful
    history that early; both good and bad coins score near-zero.
    Bonus-only: never returns negative pts.
    """
    if age_minutes < 25:
        return None, 0

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
            return None, 0

        data = r.json()
        token = data[0] if isinstance(data, list) else data
        raw = token.get("organicScore")
        if raw is None:
            return None, 0

        score = float(raw)

        if score >= 50:
            pts = 7
        elif score >= 25:
            pts = 5
        elif score >= 10:
            pts = 2
        else:
            pts = 0   # near-zero even for older tokens = all bots, no bonus

        return score, pts

    except Exception:
        return None, 0

# ── Stablecoins / non-meme tokens — always reject ─────────────────────────────
BLACKLISTED_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",      # Wrapped SOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # ETH (Wormhole)
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",  # BTC (Sollet)
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK (established, not new meme)
}

BLACKLISTED_KEYWORDS = {
    "usd", "usdc", "usdt", "tether", "stablecoin",
    "bitcoin", "btc", "ethereum", "eth", "wrapped",
    "solana", "wormhole", "msol", "jito", "jitosol",
}


def score_token(token: dict) -> dict:
    """
    Master scoring function.
    Input: token dict with keys from scanner.py
    Output: {
        "score": 0-100,
        "buy": bool,
        "hold": bool,
        "breakdown": dict,
        "reject_reason": str | None
    }
    """
    mint        = token.get("mint", "")
    name        = token.get("name", "")
    ticker      = token.get("ticker", "")
    twitter     = token.get("twitter")
    website     = token.get("website")
    liquidity   = token.get("liquidity_usd", 0)
    volume_5m   = token.get("volume_5m", 0)
    volume_1h   = token.get("volume_1h", 0)
    holders     = token.get("holders", 0)
    dev_pct          = token.get("dev_wallet_pct")
    top10_pct        = token.get("top10_pct")
    top_holder_pct   = token.get("top_holder_pct")
    is_dev_top_holder = token.get("is_dev_top_holder", False)
    mint_auth   = token.get("mint_authority_revoked", False)
    freeze_auth = token.get("freeze_authority_revoked", False)
    lp_locked   = token.get("lp_locked_pct", 0)
    age_min     = token.get("age_minutes", 0)
    buy_txns    = token.get("buy_txns_5m", 0)
    sell_txns   = token.get("sell_txns_5m", 0)
    bonding_pct = token.get("bonding_curve_pct", 0)   # pump.fun only

    breakdown = {}

    # ── Hard reject conditions (instant skip) ──────────────────────────────

    # Blacklisted mints (stablecoins, wrapped assets, established non-memes)
    if mint in BLACKLISTED_MINTS:
        return _reject(f"Blacklisted token — not a meme coin ({mint[:8]}...)")

    # Blacklisted keywords in name or ticker
    name_lower   = name.lower()
    ticker_lower = ticker.lower()
    for kw in BLACKLISTED_KEYWORDS:
        if kw in name_lower or kw in ticker_lower:
            return _reject(f"Non-meme token detected — keyword '{kw}' in name/ticker")

    # Age — too old (missed the run) or too new (unstable)
    if age_min > 120:
        return _reject(f"Token too old ({age_min:.0f} min) — missed the run")
    if age_min < 3:
        return _reject(f"Token too new ({age_min:.0f} min) — wait for stability")

    if not mint_auth:
        return _reject("Mint authority NOT revoked — rug risk")
    if not freeze_auth:
        return _reject("Freeze authority NOT revoked — rug risk")
    # Dev-aware top holder check: stricter if top holder is the creator
    if top_holder_pct is not None:
        if is_dev_top_holder and top_holder_pct > 10:
            return _reject(f"Dev holds {top_holder_pct:.1f}% — too centralised (dev limit: 10%)")
        elif not is_dev_top_holder and top_holder_pct > 25:
            return _reject(f"Single whale holds {top_holder_pct:.1f}% — rug risk (whale limit: 25%)")
    elif dev_pct is not None and dev_pct > 10:
        return _reject(f"Top holder {dev_pct:.1f}% exceeds safe limit")
    if liquidity < 400 and volume_5m < 200 and volume_1h < 300:
        return _reject(f"Liquidity too low (${liquidity:,.0f})")
    if holders < 5 and volume_5m < 50 and volume_1h < 200:
        return _reject(f"Only {holders} holders — too early/fake")
    if top10_pct is not None and top10_pct > 80:
        return _reject(f"Top 10 wallets hold {top10_pct:.0f}% — centralised")

    # Heavy selling pressure (65%+ sells with meaningful activity)
    total_txns = buy_txns + sell_txns
    if total_txns >= 20:
        sell_ratio = sell_txns / total_txns
        if sell_ratio >= 0.65:
            return _reject(
                f"Heavy selling pressure ({buy_txns}B/{sell_txns}S)"
            )

    # Already pumped hard — we missed the run, buying the top
    price_change_1h = token.get("price_change_1h", 0)
    if price_change_1h > 100:
        return _reject(f"Already pumped +{price_change_1h:.0f}% in 1h — missed the run")

    # ── On-chain safety (50 pts) ───────────────────────────────────────────
    onchain = 0

    # Liquidity
    if liquidity >= 100_000:
        onchain += 18
    elif liquidity >= 50_000:
        onchain += 13
    elif liquidity >= 20_000:
        onchain += 8
    elif liquidity >= 10_000:
        onchain += 5
    elif liquidity >= 2_000:
        onchain += 3
    elif liquidity >= 500:
        onchain += 1
    breakdown["liquidity"] = f"${liquidity:,.0f} (+{onchain})"

    # LP locked
    # pump.fun auto-locks LP on graduation — not a real signal, give 0 pts
    # Only reward manual locks on non-pump.fun tokens
    lp_pts = 0
    is_pumpfun = bonding_pct > 0 or lp_locked == 100  # pump.fun always shows 100%
    if not is_pumpfun:
        if lp_locked >= 80:
            lp_pts = 10
        elif lp_locked >= 50:
            lp_pts = 6
        elif lp_locked >= 20:
            lp_pts = 3
    onchain += lp_pts
    breakdown["lp_locked"] = f"{lp_locked:.0f}% (+{lp_pts}){'[pumpfun-ignored]' if is_pumpfun else ''}"

    # Dev wallet
    dev_pts = 0
    if dev_pct is not None:
        if dev_pct <= 2:
            dev_pts = 5
        elif dev_pct <= 5:
            dev_pts = 3
        elif dev_pct <= 10:
            dev_pts = 2
        elif dev_pct < 25:
            dev_pts = 1   # passed hard reject, deserves 1 point
        breakdown["dev_wallet"] = f"{dev_pct:.1f}% (+{dev_pts})"
    else:
        breakdown["dev_wallet"] = "Unknown (+0)"
    onchain += dev_pts

    # top single holder (info only — hard reject already caught >25% above)
    dev_label = " (dev)" if is_dev_top_holder else " (whale)"
    breakdown["top_holder"] = f"{top_holder_pct:.1f}%{dev_label}" if top_holder_pct is not None else "Unknown"

    # Top 10 concentration — tighter thresholds to penalise whale concentration
    # POKEUS had 47% top10 and dumped — old scoring gave +6, new gives +2
    top_pts = 0
    if top10_pct is not None:
        if top10_pct <= 30:
            top_pts = 10
        elif top10_pct <= 40:
            top_pts = 6
        elif top10_pct <= 55:
            top_pts = 2
        elif top10_pct <= 65:
            top_pts = 0   # was +2, now 0 — too concentrated
        breakdown["top10"] = f"{top10_pct:.0f}% (+{top_pts})"
    else:
        breakdown["top10"] = "Unknown (+0)"
    onchain += top_pts

    breakdown["onchain_total"] = onchain

    # ── Holder / volume momentum (25 pts) ─────────────────────────────────
    momentum = 0

    holder_pts = 0
    if holders >= 300:
        holder_pts = 13
    elif holders >= 150:
        holder_pts = 9
    elif holders >= 75:
        holder_pts = 6
    elif holders >= 30:
        holder_pts = 4
    momentum += holder_pts
    breakdown["holders"] = f"{holders} (+{holder_pts})"

    # Buy/sell ratio
    ratio_pts = 0
    total_txns = buy_txns + sell_txns
    if total_txns > 0:
        buy_ratio = buy_txns / total_txns
        if buy_ratio >= 0.70:
            ratio_pts = 8
        elif buy_ratio >= 0.55:
            ratio_pts = 5
        elif buy_ratio >= 0.45:
            ratio_pts = 2
        elif buy_ratio < 0.35:
            ratio_pts = -3   # more selling than buying
    momentum += ratio_pts
    breakdown["buy_sell_ratio"] = f"{buy_txns}B/{sell_txns}S (+{ratio_pts})"

    # Volume 5m
    vol_pts = 0
    if volume_5m >= 50_000:
        vol_pts = 7
    elif volume_5m >= 10_000:
        vol_pts = 4
    elif volume_5m >= 2_000:
        vol_pts = 2
    elif volume_5m >= 500:
        vol_pts = 1
    momentum += vol_pts
    breakdown["volume_5m"] = f"${volume_5m:,.0f} (+{vol_pts})"

    # Price momentum — strong 1h pump is a buy signal
    price_change_1h = token.get('price_change_1h', 0)
    price_pts = 0
    if price_change_1h >= 50:
        price_pts = 5
    elif price_change_1h >= 20:
        price_pts = 3
    elif price_change_1h >= 10:
        price_pts = 1
    momentum += price_pts
    breakdown["price_momentum"] = f"{price_change_1h:.1f}% (+{price_pts})"

    breakdown["momentum_total"] = momentum

    # ── Pump.fun bonding curve (5 pts) ────────────────────────────────────
    # RugCheck often returns 0 — fall back to direct RPC fetch if so.
    # Only one getAccountInfo call per token that reaches scorer.
    if bonding_pct == 0:
        bonding_pct = _get_bonding_curve_pct(mint)

    bonding_pts = 0
    if bonding_pct >= 60:
        bonding_pts = 5
    elif bonding_pct >= 30:
        bonding_pts = 3
    elif bonding_pct >= 10:
        bonding_pts = 1
    breakdown["bonding_curve"] = f"{bonding_pct:.0f}% (+{bonding_pts})"

    # ── Twitter (10 pts max) ───────────────────────────────────────────────
    tw = score_twitter(twitter)
    tw_score = min(tw["score"], 10)  # capped at 10 (search-based check only)

    # Pre-launch Twitter bonus (DuckDuckGo search for coin name/ticker)
    if ticker:
        pre = search_pre_launch_twitter(name, ticker)
        pre_bonus = pre["score_bonus"]
        tw_score = min(tw_score + pre_bonus, 10)
        breakdown["twitter_pre_launch"] = pre["reason"]
    else:
        pre_bonus = 0

    # Zero out twitter score if unverifiable — unverified links shouldn't give points
    if "unverifiable" in tw.get("reason", "").lower() or "not found" in tw.get("reason", "").lower():
        tw_score = 0
        pre_bonus = 0
    breakdown["twitter"] = f"{tw['reason']} (+{tw_score})"

    # ── Website (10 pts max, capped at 5 for thin sites) ───────────────────
    ws = score_website(website)
    ws_score = ws["score"]
    # Thin content sites score capped at 5 — real projects have rich content
    reason_lower = ws.get("reason", "").lower()
    if "thin" in reason_lower or ("some content" in reason_lower and ws_score >= 8):
        ws_score = min(ws_score, 5)
    ws_score = min(ws_score, 10)
    breakdown["website"] = f"{ws['reason']} (+{ws_score})"

    # ── No social presence penalty ─────────────────────────────────────────
    # If coin has neither Twitter nor website — extra -5 penalty.
    # Legitimate meme coins almost always have at least one social channel.
    no_social_penalty = 0
    if tw_score == 0 and ws_score == 0:
        no_social_penalty = -5
    breakdown["no_social_penalty"] = f"{no_social_penalty}"

    # ── Age penalty ────────────────────────────────────────────────────────
    # Hard rejects handle age < 3 and age > 120.
    # Soft penalty for tokens between 5–15 min (still stabilising) or 60–120 min (going stale).
    age_penalty = 0
    if age_min < 15:
        age_penalty = -3   # very new — slight penalty, was -5
    elif age_min > 60:
        age_penalty = -5   # getting stale — needs strong on-chain to compensate
    breakdown["age_penalty"] = f"{age_min:.0f}min ({age_penalty})"

    # ── 1h price change (info only) ────────────────────────────────────────
    breakdown["price_change_1h"] = f"{token.get('price_change_1h', 0):.1f}%"

    # ── Jupiter organic score (7 pts max, bonus only) ─────────────────────
    # Skipped for tokens < 25 min old — score is meaningless that early.
    # Uses config.JUPITER_API_KEY (same key as trader.py).
    organic_raw, organic_pts = _get_organic_score(mint, age_min)
    if organic_raw is not None:
        breakdown["organic_score"] = f"{organic_raw:.1f} (+{organic_pts})"
    else:
        breakdown["organic_score"] = f"skipped (age {age_min:.0f}m < 25m) (+0)" if age_min < 25 else "unavailable (+0)"

    # ── Total ──────────────────────────────────────────────────────────────
    raw_score = onchain + momentum + bonding_pts + tw_score + ws_score + no_social_penalty + age_penalty + organic_pts
    score = max(0, min(100, raw_score))
    breakdown["final"] = score

    buy  = score >= config.MIN_SCORE_TO_BUY
    hold = config.HOLD_GOOD_COINS and score >= config.HOLD_MIN_SCORE

    return {
        "score": score,
        "buy": buy,
        "hold": hold,
        "breakdown": breakdown,
        "reject_reason": None,
        "twitter_data": tw.get("data", {}),
        "website_data": ws.get("data", {}),
    }


def _reject(reason: str) -> dict:
    return {
        "score": 0,
        "buy": False,
        "hold": False,
        "breakdown": {},
        "reject_reason": reason,
        "twitter_data": {},
        "website_data": {},
    }
