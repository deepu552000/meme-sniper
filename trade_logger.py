# trade_logger.py — Log every completed trade and compute PnL stats

import json
import os
import time

TRADE_LOG_FILE = "trade_log.json"


def log_trade(
    mint: str,
    name: str,
    ticker: str,
    score: int,
    hold: bool,
    buy_time: float,
    sell_time: float,
    buy_usd: float,
    sell_usd: float,
    pnl_usd: float,
    pnl_pct: float,
    sell_reason: str,
    manual: bool = False,
    tx_sell: str = "",
):
    """Append one completed trade to trade_log.json."""
    entry = {
        "time_buy":    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(buy_time)),
        "time_sell":   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sell_time)),
        "held_min":    round((sell_time - buy_time) / 60, 1),
        "mint":        mint,
        "name":        name,
        "ticker":      ticker,
        "score":       score,
        "hold":        hold,
        "buy_usd":     round(buy_usd, 4),
        "sell_usd":    round(sell_usd, 4),
        "pnl_usd":     round(pnl_usd, 4),
        "pnl_pct":     round(pnl_pct, 2),
        "sell_reason": sell_reason,
        "manual":      manual,   # True = bot gave up, you sold manually on Jupiter
        "tx_sell":     tx_sell,
    }
    try:
        with open(TRADE_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[trade_logger] Could not write trade log: {e}")


def get_stats(days: int = 30) -> dict:
    """
    Read trade_log.json and compute summary stats for the last N days.
    Returns a dict with counts, PnL, win rate etc.
    """
    if not os.path.exists(TRADE_LOG_FILE):
        return {"total": 0}

    cutoff = time.time() - (days * 86400)
    trades = []

    try:
        with open(TRADE_LOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Parse sell time to filter by date
                    sell_ts = time.mktime(time.strptime(entry["time_sell"], "%Y-%m-%d %H:%M:%S"))
                    if sell_ts >= cutoff:
                        trades.append(entry)
                except Exception:
                    continue
    except Exception as e:
        print(f"[trade_logger] Could not read trade log: {e}")
        return {"total": 0}

    if not trades:
        return {"total": 0, "days": days}

    winners  = [t for t in trades if t["pnl_usd"] > 0 and not t["manual"]]
    losers   = [t for t in trades if t["pnl_usd"] <= 0 and not t["manual"]]
    manuals  = [t for t in trades if t["manual"]]
    auto     = [t for t in trades if not t["manual"]]

    total_pnl    = sum(t["pnl_usd"] for t in auto)
    manual_pnl   = sum(t["pnl_usd"] for t in manuals)  # estimated at bot-exit price

    best  = max(auto, key=lambda t: t["pnl_usd"], default=None)
    worst = min(auto, key=lambda t: t["pnl_usd"], default=None)

    avg_win  = sum(t["pnl_usd"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl_usd"] for t in losers)  / len(losers)  if losers  else 0

    return {
        "days":        days,
        "total":       len(trades),
        "auto":        len(auto),
        "manual":      len(manuals),
        "winners":     len(winners),
        "losers":      len(losers),
        "win_rate":    round(len(winners) / len(auto) * 100, 1) if auto else 0,
        "net_pnl":     round(total_pnl, 2),
        "manual_pnl":  round(manual_pnl, 2),   # estimated
        "avg_win":     round(avg_win, 2),
        "avg_loss":    round(avg_loss, 2),
        "best_trade":  best,
        "worst_trade": worst,
    }


def format_stats_message(days: int = 30) -> str:
    """Format stats as a Telegram message."""
    s = get_stats(days)

    if s["total"] == 0:
        return f"📊 No completed trades in the last {days} days yet."

    best  = s.get("best_trade")
    worst = s.get("worst_trade")

    best_str  = f"+${best['pnl_usd']:.2f} ({best['ticker']})"   if best  else "—"
    worst_str = f"${worst['pnl_usd']:.2f} ({worst['ticker']})"  if worst else "—"

    manual_note = ""
    if s["manual"] > 0:
        manual_note = (
            f"\n⚠️ Manual exits: {s['manual']} (PnL ~${s['manual_pnl']:+.2f} est.)"
        )

    return (
        f"📊 <b>Trade Stats — last {days} days</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Total trades:  {s['total']} ({s['auto']} auto{manual_note and ', ' + str(s['manual']) + ' manual'})\n"
        f"Winners:       {s['winners']} | Losers: {s['losers']}\n"
        f"Win rate:      {s['win_rate']}%\n"
        f"\n"
        f"Net PnL:       ${s['net_pnl']:+.2f}\n"
        f"Avg winner:    +${s['avg_win']:.2f}\n"
        f"Avg loser:     ${s['avg_loss']:.2f}\n"
        f"\n"
        f"Best trade:    {best_str}\n"
        f"Worst trade:   {worst_str}"
        f"{manual_note}"
    )
