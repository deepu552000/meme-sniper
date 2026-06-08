# monitor.py — Track open positions and trigger take-profit / stop-loss

import time
import json
import os
import requests
import config
import trader
import telegram_bot as tg
import hold_manager

HEADERS = {"User-Agent": "Mozilla/5.0 MemeSniper/1.0"}

POSITIONS_FILE = "positions.json"

def _load_positions() -> dict:
    """Load open positions from disk on startup."""
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE, "r") as f:
            data = json.load(f)
        print(f"[monitor] Restored {len(data)} open position(s) from disk")
        return data
    except Exception as e:
        print(f"[monitor] Could not load positions: {e}")
        return {}

def _save_positions():
    """Persist current positions to disk."""
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        print(f"[monitor] Could not save positions: {e}")

# positions: { mint: { "entry_price": float, "token_amount": float,
#                       "buy_time": float, "score": int, "hold": bool,
#                       "name": str, "ticker": str } }
positions: dict[str, dict] = _load_positions()


def add_position(mint: str, entry_price: float, token_amount: float,
                 score: int, hold: bool, name: str, ticker: str):
    positions[mint] = {
        "entry_price":  entry_price,
        "token_amount": token_amount,
        "buy_time":     time.time(),
        "score":        score,
        "hold":         hold,
        "name":         name,
        "ticker":       ticker,
        "peak_price":   entry_price,
        "high_water":   entry_price,
        "sell_attempts": 0,
    }
    _save_positions()
    print(f"[monitor] Position added: {ticker} @ ${entry_price:.8f}")


def remove_position(mint: str):
    positions.pop(mint, None)
    _save_positions()


def check_positions():
    """
    Called on a loop. Checks all open positions and sells if conditions met.
    """
    if not positions:
        return

    for mint, pos in list(positions.items()):
        try:
            # Skip positions flagged for manual intervention
            if pos.get("manual"):
                continue

            current_price = _get_current_price(mint)
            if not current_price:
                continue

            entry   = pos["entry_price"]
            hold    = pos["hold"]
            elapsed = (time.time() - pos["buy_time"]) / 3600   # hours
            pnl_pct = (current_price - entry) / entry

            # Update peak
            if current_price > pos["high_water"]:
                pos["high_water"] = current_price
                _save_positions()

            drawdown_from_peak = (current_price - pos["high_water"]) / pos["high_water"]

            # ── Sell conditions ────────────────────────────────────────────

            sell_reason = None

            # 1. Stop loss — always (both hold and quick flip)
            if pnl_pct <= -config.STOP_LOSS:
                sell_reason = f"🔴 Stop loss hit ({pnl_pct*100:.1f}%)"

            # 2. Take profit — quick flip coins ONLY (score < HOLD_MIN_SCORE)
            #    Hold coins skip TP entirely — trailing stop + 24h rule handles exit.
            #    Intentional: we want hold coins to run to 100%+ for long-hold promotion.
            elif not hold and pnl_pct >= config.TAKE_PROFIT:
                sell_reason = f"✅ Take profit ({pnl_pct*100:.1f}%)"

            # 3. Hold coins: exit after 24h (promotes to long-hold if >= 100%)
            elif hold and elapsed >= 24:
                # Check if eligible for long-hold promotion (still >= 100% profit and slots available)
                if pnl_pct >= 1.0 and hold_manager.slots_available():
                    promoted = hold_manager.promote_to_long_hold(mint, pos, current_price)
                    if promoted:
                        remove_position(mint)
                        continue
                    # If promotion failed (shouldn't happen but be safe), fall through to normal sell
                sell_reason = f"⏰ 24h hold exit ({pnl_pct*100:.1f}%)"

            # 4. Trailing stop — always active, no time restriction.
            #    Fires when price drops config.TRAILING_STOP % from peak.
            #    Catches big pumps on the way down (e.g. +600% pumps -25% → sell at ~+450%).
            elif drawdown_from_peak <= -config.TRAILING_STOP:
                sell_reason = f"⚠️ Trailing stop ({drawdown_from_peak*100:.1f}% from peak)"

            # 5. Volume collapse — check buy/sell ratio
            elif _detect_dump_signal(mint):
                sell_reason = "⚠️ Large wallet dump detected"

            if sell_reason:
                _execute_sell(mint, pos, current_price, pnl_pct, sell_reason)

        except Exception as e:
            print(f"[monitor] Error checking {mint}: {e}")


# ─── Fast-poll for newly opened positions (Fix 1) ────────────────────────────

YOUNG_POSITION_MAX_AGE_SEC = 300   # fast-poll for first 5 minutes after buy

def check_young_positions():
    """
    Called every 5s from main loop.
    Only checks positions under YOUNG_POSITION_MAX_AGE_SEC old.
    Catches fast dumps/graduations that the slow monitor cycle would miss.
    """
    if not positions:
        return

    now = time.time()
    for mint, pos in list(positions.items()):
        try:
            if pos.get("manual"):
                continue
            age_sec = now - pos["buy_time"]
            if age_sec > YOUNG_POSITION_MAX_AGE_SEC:
                continue   # regular check_positions handles older ones

            current_price = _get_current_price(mint)
            if not current_price:
                continue

            entry   = pos["entry_price"]
            pnl_pct = (current_price - entry) / entry

            # Update peak
            if current_price > pos["high_water"]:
                pos["high_water"] = current_price
                _save_positions()

            drawdown_from_peak = (current_price - pos["high_water"]) / pos["high_water"]

            sell_reason = None

            if pnl_pct <= -config.STOP_LOSS:
                sell_reason = f"🔴 Stop loss hit ({pnl_pct*100:.1f}%) [fast-poll]"
            elif drawdown_from_peak <= -config.TRAILING_STOP:
                sell_reason = f"⚠️ Trailing stop ({drawdown_from_peak*100:.1f}% from peak) [fast-poll]"
            elif _detect_dump_signal(mint):
                sell_reason = "⚠️ Large wallet dump detected [fast-poll]"

            if sell_reason:
                _execute_sell(mint, pos, current_price, pnl_pct, sell_reason)

        except Exception as e:
            print(f"[monitor] Error in fast-poll for {mint}: {e}")


# ─── Price fetcher ────────────────────────────────────────────────────────────

def _get_current_price(mint: str) -> float | None:
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
        return float(best.get("priceUsd", 0) or 0)
    except Exception:
        return None


def _detect_dump_signal(mint: str) -> bool:
    """
    Check if a large holder (whale) is dumping.
    Uses DexScreener recent txns — looks for a sell >10% of liquidity in 1 min.
    """
    try:
        r = requests.get(
            f"{config.DEXSCREENER_URL}/latest/dex/tokens/{mint}",
            headers=HEADERS,
            timeout=8
        )
        r.raise_for_status()
        pairs = r.json().get("pairs", [])
        if not pairs:
            return False
        best = pairs[0]

        txns_1m = best.get("txns", {}).get("m1", {})
        buys  = txns_1m.get("buys", 1)
        sells = txns_1m.get("sells", 0)

        if sells > 0 and buys == 0:
            return True
        if sells > 0 and buys > 0 and (sells / (buys + sells)) > 0.85:
            return True

        change_1m = best.get("priceChange", {}).get("m1", 0)
        if change_1m and float(change_1m) < -25:
            return True

    except Exception:
        pass
    return False


# ─── Execute sell ─────────────────────────────────────────────────────────────

MAX_SELL_ATTEMPTS = 3

def _execute_sell(mint, pos, current_price, pnl_pct, reason):
    ticker = pos["ticker"]
    name   = pos["name"]

    # Already marked manual — skip silently, no more alerts or retries
    if pos.get("manual"):
        return

    attempt = pos.get("sell_attempts", 0)  # don't increment yet
    print(f"[monitor] SELL triggered for {ticker}: {reason} (attempt {attempt+1}/{MAX_SELL_ATTEMPTS})")

    result = trader.sell_token(mint, pos["token_amount"])

    if result["success"]:
        sol_usd = trader.get_sol_price_usd()

        # PumpPortal returns sol_received=0 — calculate profit from PnL% instead
        if result["sol_received"] > 0:
            profit_usd = (result["sol_received"] * sol_usd) - config.BUY_AMOUNT_USD
        else:
            profit_usd = config.BUY_AMOUNT_USD * pnl_pct

        tg.send_sell_result(
            mint=mint,
            ticker=ticker,
            name=name,
            tx_result=result,
            reason=f"{reason} | Entry: ${pos['entry_price']:.8f} → Exit: ${current_price:.8f} (${profit_usd:+.2f})",
            pnl_pct=pnl_pct * 100,
        )
        remove_position(mint)

    else:
        # Only increment on actual failure
        attempt += 1
        pos["sell_attempts"] = attempt
        _save_positions()

        if attempt >= MAX_SELL_ATTEMPTS:
            # Max attempts reached — mark manual, send final alert with links
            pos["manual"] = True
            _save_positions()
            tg.send_sell_failed_alert(mint, result["error"])
        else:
            tg.send(
                f"⚠️ Sell FAILED for {ticker} (attempt {attempt}/{MAX_SELL_ATTEMPTS})\n"
                f"Error: {result['error']}\n"
                f"PnL now: {pnl_pct*100:+.1f}%\n"
                f"Will retry next cycle."
            )


def get_open_positions_summary() -> str:
    if not positions:
        return "No open positions."
    lines = ["📊 Open Positions:"]
    for mint, pos in positions.items():
        price = _get_current_price(mint) or 0
        pnl   = ((price - pos["entry_price"]) / pos["entry_price"] * 100) if pos["entry_price"] else 0
        hold_tag = " [HOLD]" if pos["hold"] else ""
        manual_tag = " ⚠️ MANUAL" if pos.get("manual") else ""
        lines.append(
            f"  • {pos['ticker']}{hold_tag}{manual_tag}: {pnl:+.1f}% | "
            f"${price:.8f} | "
            f"{(time.time()-pos['buy_time'])/3600:.1f}h held"
        )
    return "\n".join(lines)
