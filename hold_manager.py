# hold_manager.py — Long-term hold tier
#
# Tokens are PROMOTED here from monitor.py when:
#   - The 24h hold exit fires AND
#   - PnL >= 100% (i.e. price is still >= 2x entry) AND
#   - Long-hold slots < MAX_LONG_HOLD_POSITIONS
#
# Once here, a token is held indefinitely with these rules:
#   - Sell immediately if price drops below LONG_HOLD_EXIT_THRESHOLD (default 90% = 0.9x gain)
#     i.e. if entry was $0.01, sell if price < $0.019  (90% profit floor, not 100%)
#     The 10% buffer prevents a brief dip below 2x from triggering a premature exit.
#   - Sell on /holdsell TICKER command from Telegram at any time
#   - Monitoring every LONG_HOLD_MONITOR_SEC (default 300 = 5 minutes)
#
# Separate persistence: hold_positions.json (never mixed with positions.json)

import time
import json
import os
import requests
import config
import trader
import telegram_bot as tg

HEADERS = {"User-Agent": "Mozilla/5.0 MemeSniper/1.0"}

HOLD_POSITIONS_FILE = "hold_positions.json"

# Max simultaneous long-hold positions
MAX_LONG_HOLD_POSITIONS = 3

# Exit if PnL drops below this from entry (90% profit floor = 0.9x entry)
# e.g. entry $0.01 → hold as long as price >= $0.019, sell if < $0.019
LONG_HOLD_EXIT_THRESHOLD = 0.90   # 90% profit minimum to stay in

# Monitor interval in seconds — set in main.py loop
LONG_HOLD_MONITOR_SEC = 300       # 5 minutes


# ─── Persistence ─────────────────────────────────────────────────────────────

def _load_hold_positions() -> dict:
    if not os.path.exists(HOLD_POSITIONS_FILE):
        return {}
    try:
        with open(HOLD_POSITIONS_FILE, "r") as f:
            data = json.load(f)
        print(f"[hold_manager] Restored {len(data)} long-hold position(s) from disk")
        return data
    except Exception as e:
        print(f"[hold_manager] Could not load hold positions: {e}")
        return {}


def _save_hold_positions():
    try:
        with open(HOLD_POSITIONS_FILE, "w") as f:
            json.dump(hold_positions, f, indent=2)
    except Exception as e:
        print(f"[hold_manager] Could not save hold positions: {e}")


# hold_positions: { mint: { entry_price, token_amount, promoted_price,
#                            promoted_time, buy_time, score, name, ticker,
#                            peak_price, sell_attempts, manual } }
hold_positions: dict[str, dict] = _load_hold_positions()


# ─── Public API ───────────────────────────────────────────────────────────────

def slots_available() -> bool:
    return len(hold_positions) < MAX_LONG_HOLD_POSITIONS


def promote_to_long_hold(mint: str, pos: dict, current_price: float):
    """
    Called from monitor.py when 24h exit fires and PnL >= 100%.
    Moves the position from positions.json tracking to hold_positions.json.
    The caller (monitor.py) is responsible for calling monitor.remove_position(mint)
    after this returns True.
    Returns True if promoted, False if slots full.
    """
    if not slots_available():
        print(f"[hold_manager] No long-hold slots available for {pos.get('ticker','?')}")
        return False

    slot_num = len(hold_positions) + 1
    pnl_pct  = (current_price - pos["entry_price"]) / pos["entry_price"] * 100

    hold_positions[mint] = {
        "entry_price":    pos["entry_price"],
        "token_amount":   pos["token_amount"],
        "promoted_price": current_price,
        "promoted_pnl":   pnl_pct,
        "promoted_time":  time.time(),
        "buy_time":       pos["buy_time"],
        "score":          pos.get("score", 0),
        "name":           pos["name"],
        "ticker":         pos["ticker"],
        "peak_price":     current_price,
        "sell_attempts":  0,
        "manual":         False,
        "slot":           slot_num,
    }
    _save_hold_positions()

    # Compute exit floor price for the alert message
    exit_floor = pos["entry_price"] * (1 + LONG_HOLD_EXIT_THRESHOLD)

    tg.send(
        f"🌟 <b>PROMOTED TO LONG-HOLD</b> [{slot_num}/{MAX_LONG_HOLD_POSITIONS}]\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Token:  {pos['name']} (${pos['ticker']})\n"
        f"Entry:  ${pos['entry_price']:.8f}\n"
        f"Now:    ${current_price:.8f} (+{pnl_pct:.1f}%)\n"
        f"Floor:  ${exit_floor:.8f} ({LONG_HOLD_EXIT_THRESHOLD*100:.0f}% profit minimum)\n"
        f"\n"
        f"📌 Will hold indefinitely above floor.\n"
        f"Use /holdsell {pos['ticker']} to exit manually.\n"
        f"🔗 <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a>"
    )
    print(f"[hold_manager] Promoted {pos['ticker']} to long-hold slot {slot_num} @ ${current_price:.8f} (+{pnl_pct:.1f}%)")
    return True


def remove_hold_position(mint: str):
    hold_positions.pop(mint, None)
    _save_hold_positions()


# ─── Monitor loop (called from main.py every 5 min) ──────────────────────────

MAX_SELL_ATTEMPTS = 3

def check_long_holds():
    """
    Main monitoring loop for long-hold positions.
    Called every LONG_HOLD_MONITOR_SEC from main.py.
    Sell condition: price drops below entry * (1 + LONG_HOLD_EXIT_THRESHOLD).
    """
    if not hold_positions:
        return

    for mint, pos in list(hold_positions.items()):
        try:
            if pos.get("manual"):
                continue

            current_price = _get_current_price(mint)
            if not current_price:
                print(f"[hold_manager] Could not fetch price for {pos['ticker']} — skipping")
                continue

            entry    = pos["entry_price"]
            pnl_pct  = (current_price - entry) / entry
            exit_floor_pct = LONG_HOLD_EXIT_THRESHOLD  # e.g. 0.90

            # Update peak
            if current_price > pos["peak_price"]:
                pos["peak_price"] = current_price
                _save_hold_positions()

            held_days = (time.time() - pos["promoted_time"]) / 86400

            # ── Sell condition: price has fallen below the 90% profit floor ──
            if pnl_pct < exit_floor_pct:
                reason = (
                    f"📉 Long-hold floor breached ({pnl_pct*100:+.1f}% < {exit_floor_pct*100:.0f}% floor) "
                    f"after {held_days:.1f} days"
                )
                _execute_long_sell(mint, pos, current_price, pnl_pct, reason)

        except Exception as e:
            print(f"[hold_manager] Error checking {pos.get('ticker','?')}: {e}")


def manual_sell(mint: str) -> bool:
    """
    Triggered by /holdsell command from Telegram.
    Returns True if position found and sell attempted.
    """
    pos = hold_positions.get(mint)
    if not pos:
        return False

    current_price = _get_current_price(mint) or 0
    pnl_pct       = (current_price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] else 0
    reason        = f"📲 Manual /holdsell command (held {(time.time()-pos['promoted_time'])/86400:.1f} days)"
    _execute_long_sell(mint, pos, current_price, pnl_pct, reason)
    return True


def find_mint_by_ticker(ticker: str) -> str | None:
    """Look up mint address by ticker (case-insensitive). Used by /holdsell."""
    ticker_lower = ticker.lower().lstrip("$")
    for mint, pos in hold_positions.items():
        if pos.get("ticker", "").lower() == ticker_lower:
            return mint
    return None


# ─── Sell execution ───────────────────────────────────────────────────────────

def _execute_long_sell(mint, pos, current_price, pnl_pct, reason):
    ticker = pos["ticker"]
    name   = pos["name"]

    if pos.get("manual"):
        return

    attempt = pos.get("sell_attempts", 0)
    print(f"[hold_manager] SELL triggered for {ticker}: {reason} (attempt {attempt+1}/{MAX_SELL_ATTEMPTS})")

    result = trader.sell_token(mint, pos["token_amount"])

    if result["success"]:
        sol_usd = trader.get_sol_price_usd()
        if result["sol_received"] > 0:
            profit_usd = (result["sol_received"] * sol_usd) - config.BUY_AMOUNT_USD
        else:
            profit_usd = config.BUY_AMOUNT_USD * pnl_pct

        held_days = (time.time() - pos["promoted_time"]) / 86400
        peak_gain = (pos["peak_price"] - pos["entry_price"]) / pos["entry_price"] * 100

        tg.send(
            f"💰 <b>LONG-HOLD SOLD</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Token:  {name} (${ticker})\n"
            f"Reason: {reason}\n"
            f"Entry:  ${pos['entry_price']:.8f}\n"
            f"Exit:   ${current_price:.8f} ({pnl_pct*100:+.1f}%)\n"
            f"Peak:   +{peak_gain:.1f}%\n"
            f"Held:   {held_days:.1f} days\n"
            f"P&L:    ${profit_usd:+.2f}\n"
            f"TX: <a href='https://solscan.io/tx/{result['tx']}'>View on Solscan</a>"
        )
        remove_hold_position(mint)

    else:
        attempt += 1
        pos["sell_attempts"] = attempt
        _save_hold_positions()

        if attempt >= MAX_SELL_ATTEMPTS:
            pos["manual"] = True
            _save_hold_positions()
            tg.send(
                f"🚨 <b>LONG-HOLD SELL FAILED — ALL RETRIES EXHAUSTED</b>\n"
                f"Token: {name} (${ticker})\n"
                f"Mint: <code>{mint}</code>\n"
                f"Error: {result['error']}\n\n"
                f"⚠️ <b>Manual sell required!</b>\n"
                f"🔗 <a href='https://jup.ag/swap/SOL-{mint}'>Sell on Jupiter</a> | "
                f"<a href='https://dexscreener.com/solana/{mint}'>DexScreener</a>"
            )
        else:
            tg.send(
                f"⚠️ Long-hold sell FAILED for {ticker} (attempt {attempt}/{MAX_SELL_ATTEMPTS})\n"
                f"Error: {result['error']}\n"
                f"PnL now: {pnl_pct*100:+.1f}%\n"
                f"Will retry next cycle."
            )


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


# ─── Summary (for /status and /positions commands) ───────────────────────────

def get_long_hold_summary() -> str:
    if not hold_positions:
        return "No long-hold positions."
    lines = [f"🌟 Long-Hold Positions ({len(hold_positions)}/{MAX_LONG_HOLD_POSITIONS}):"]
    for mint, pos in hold_positions.items():
        price     = _get_current_price(mint) or 0
        pnl       = ((price - pos["entry_price"]) / pos["entry_price"] * 100) if pos["entry_price"] else 0
        held_days = (time.time() - pos["promoted_time"]) / 86400
        floor_pct = LONG_HOLD_EXIT_THRESHOLD * 100
        manual_tag = " ⚠️ MANUAL" if pos.get("manual") else ""
        lines.append(
            f"  ★ {pos['ticker']}{manual_tag}: {pnl:+.1f}% | "
            f"${price:.8f} | "
            f"Floor: +{floor_pct:.0f}% | "
            f"{held_days:.1f}d held"
        )
    return "\n".join(lines)
