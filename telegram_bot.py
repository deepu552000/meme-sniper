# telegram_bot.py — Send alerts + handle manual commands

import asyncio
import threading
import requests
import config

# ─── Multi-channel alert dispatcher ──────────────────────────────────────────

def send(message: str):
    """Send alert via all configured channels (Telegram, WhatsApp, Email)."""
    if getattr(config, "ALERT_TELEGRAM", True):
        _send_telegram(message)
    if getattr(config, "ALERT_WHATSAPP", False):
        _send_whatsapp(message)
    if getattr(config, "ALERT_EMAIL", False):
        _send_email(message)


def _send_telegram(message: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":                  config.TELEGRAM_CHAT_ID,
                "text":                     message,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10
        )
    except Exception as e:
        print(f"[telegram] Telegram send error: {e}")


def _send_whatsapp(message: str):
    """Send via Twilio WhatsApp sandbox."""
    try:
        from twilio.rest import Client
        client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
        import re
        plain = re.sub(r'<[^>]+>', '', message)
        client.messages.create(
            body=plain,
            from_=config.TWILIO_FROM_WA,
            to=config.TWILIO_TO_WA,
        )
    except Exception as e:
        print(f"[telegram] WhatsApp send error: {e}")


def _send_email(message: str, subject: str = "Meme Sniper Alert"):
    """Send via msmtp (must be configured on VPS)."""
    try:
        import subprocess, re
        plain = re.sub(r'<[^>]+>', '', message)
        full  = f"Subject: {subject}\n\n{plain}"
        subprocess.run(
            ["msmtp", "-a", "default", config.ALERT_EMAIL_ID],
            input=full.encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception as e:
        print(f"[telegram] Email send error: {e}")


def send_scan_found(token: dict, result: dict):
    """Alert when a good coin is found (before buying)."""
    score     = result["score"]
    breakdown = result["breakdown"]
    tw_data   = result.get("twitter_data", {})

    filled = int(score / 10)
    bar    = "█" * filled + "░" * (10 - filled)

    tw_line = ""
    if tw_data.get("followers"):
        tw_line = f"\n🐦 Twitter: @{tw_data.get('handle','')} ({tw_data['followers']:,} followers)"

    msg = (
        f"🔍 <b>NEW COIN FOUND</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Name:   {token['name']} (${token['ticker']})\n"
        f"Source: {token['source']}\n"
        f"Age:    {token['age_minutes']:.0f} min\n"
        f"\n"
        f"Score: {score}/100  [{bar}]\n"
        f"{'🟢 BUYING' if result['buy'] else '🟡 Watching'}"
        f"{' [HOLD MODE]' if result.get('hold') else ''}\n"
        f"\n"
        f"💧 Liquidity: ${token.get('liquidity_usd',0):,.0f}\n"
        f"👥 Holders:   {token.get('holders',0)}\n"
        f"📊 5m Vol:    ${token.get('volume_5m',0):,.0f}\n"
        f"🔒 LP Locked: {token.get('lp_locked_pct',0):.0f}%\n"
        f"👨‍💻 Dev holds: {token.get('dev_wallet_pct',0):.1f}%"
        f"{tw_line}\n"
        f"\n"
        f"🔗 <a href='https://dexscreener.com/solana/{token['mint']}'>DexScreener</a> | "
        f"<a href='https://rugcheck.xyz/tokens/{token['mint']}'>RugCheck</a> | "
        f"<a href='https://pump.fun/{token['mint']}'>Pump.fun</a>"
    )
    send(msg)


def send_buy_result(token: dict, result: dict, tx_result: dict):
    """Alert after a buy is executed."""
    if tx_result["success"]:
        msg = (
            f"✅ <b>BOUGHT</b> {token['name']} (${token['ticker']})\n"
            f"Spent: ${config.BUY_AMOUNT_USD}\n"
            f"Score: {result['score']}/100\n"
            f"Mode:  {'HOLD 🫂' if result['hold'] else 'Quick flip ⚡'}\n"
            f"TX: <a href='https://solscan.io/tx/{tx_result['tx']}'>View on Solscan</a>"
        )
    else:
        msg = (
            f"❌ <b>BUY FAILED</b> {token['name']} (${token['ticker']})\n"
            f"Error: {tx_result['error']}"
        )
    send(msg)


def send_sell_result(mint: str, ticker: str, name: str, tx_result: dict,
                     reason: str = "", pnl_pct: float = None):
    """
    Alert after a sell is executed (success or failure).
    Call this from monitor whenever a sell is attempted.
    """
    pnl_str = f" | PnL: {pnl_pct:+.1f}%" if pnl_pct is not None else ""

    if tx_result["success"]:
        sol_recv = tx_result.get("sol_received", 0)
        msg = (
            f"💸 <b>SOLD</b> {name} (${ticker}){pnl_str}\n"
            f"Reason: {reason}\n"
            f"SOL received: {sol_recv:.4f}\n"
            f"TX: <a href='https://solscan.io/tx/{tx_result['tx']}'>View on Solscan</a>"
        )
    else:
        msg = (
            f"🚨 <b>SELL FAILED</b> {name} (${ticker}){pnl_str}\n"
            f"Reason tried: {reason}\n"
            f"Error: {tx_result['error']}\n"
            f"⚠️ <b>Position still open — manual action may be needed!</b>\n"
            f"🔗 <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a> | "
            f"<a href='https://jup.ag/swap/SOL-{mint}'>Jupiter</a>"
        )
    send(msg)


def send_sell_failed_alert(mint: str, error: str, pnl_pct: float = None):
    """
    Fired by monitor after all sell attempts are exhausted.
    Looks up position info from monitor if available.
    """
    try:
        import monitor
        pos = monitor.positions.get(mint, {})
        ticker = pos.get("ticker", "???")
        name   = pos.get("name", mint[:8] + "...")
    except Exception:
        ticker = "???"
        name   = mint[:8] + "..."

    pnl_str = f" | PnL: {pnl_pct*100:+.1f}%" if pnl_pct is not None else ""

    msg = (
        f"🚨 <b>SELL FAILED — ALL RETRIES EXHAUSTED</b>\n"
        f"Token: {name} (${ticker}){pnl_str}\n"
        f"Mint: <code>{mint}</code>\n"
        f"Error: {error}\n\n"
        f"⚠️ <b>Position still open! Manual sell required.</b>\n"
        f"🔗 <a href='https://jup.ag/swap/SOL-{mint}'>Sell on Jupiter</a> | "
        f"<a href='https://dexscreener.com/solana/{mint}'>DexScreener</a>"
    )
    send(msg)


def send_heartbeat(scan_count: int, bot_state: dict):
    import time as _t
    import wallet as w
    import trader as t
    import monitor
    sol       = w.get_sol_balance()
    sol_usd   = sol * t.get_sol_price_usd()
    positions = len(monitor.positions)
    status    = "PAUSED" if bot_state.get("paused") else "Running"
    msg = (
        "💓 <b>Bot Heartbeat</b>\n"
        + "Status:    " + status + "\n"
        + "Balance:   " + f"{sol:.4f} SOL (~${sol_usd:.2f})" + "\n"
        + "Positions: " + str(positions) + "/" + str(config.MAX_POSITIONS) + "\n"
        + "Scans:     " + str(scan_count) + " total\n"
        + "Time:      " + _t.strftime("%Y-%m-%d %H:%M:%S")
    )
    send(msg)


def send_startup():
    import wallet as w
    import trader as t
    sol = w.get_sol_balance()
    sol_usd = sol * t.get_sol_price_usd()
    # Telegram only — not WhatsApp/Email to save daily limits
    _send_telegram(
        f"🚀 <b>Meme Sniper Bot STARTED</b>\n"
        f"Wallet: <code>{w.get_pubkey()[:8]}...{w.get_pubkey()[-4:]}</code>\n"
        f"Balance: {sol:.4f} SOL (~${sol_usd:.2f})\n"
        f"Buy amount: ${config.BUY_AMOUNT_USD} | Max positions: {config.MAX_POSITIONS}\n"
        f"TP: +{config.TAKE_PROFIT*100:.0f}% | SL: -{config.STOP_LOSS*100:.0f}%\n"
        f"Scan interval: {config.SCAN_INTERVAL_SEC}s\n"
        f"Min score: {config.MIN_SCORE_TO_BUY}/100"
    )


# ─── Command listener (runs in background thread) ─────────────────────────────

_last_update_id = 0

def start_command_listener(bot_ref):
    """
    Poll Telegram for commands from user.
    Runs in a daemon thread so it doesn't block main loop.
    """
    thread = threading.Thread(target=_poll_commands, args=(bot_ref,), daemon=True)
    thread.start()


def _poll_commands(bot_ref):
    global _last_update_id
    print("[telegram] Command listener started")

    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getUpdates",
                params={"offset": _last_update_id + 1, "timeout": 20},
                timeout=30
            )
            updates = r.json().get("result", [])
            for u in updates:
                _last_update_id = u["update_id"]
                msg = u.get("message", {})
                text = msg.get("text", "").strip().lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != str(config.TELEGRAM_CHAT_ID):
                    continue

                _handle_command(text, bot_ref)

        except Exception as e:
            print(f"[telegram] Poll error: {e}")
            import time; time.sleep(5)


def _handle_command(text: str, bot_ref):
    import monitor

    # Command responses go to Telegram only — not WhatsApp/email
    reply = _send_telegram

    if text in ["/start", "/status"]:
        import wallet as w
        import trader as t
        import hold_manager
        import watchlist as wl
        sol = w.get_sol_balance()
        price = t.get_sol_price_usd()
        summary = monitor.get_open_positions_summary()
        long_hold_summary = hold_manager.get_long_hold_summary()
        wl_summary = wl.get_watchlist_summary()
        reply(
            f"🤖 Bot is running\n"
            f"SOL Balance: {sol:.4f} (${sol*price:.2f})\n\n"
            f"{summary}\n\n"
            f"{long_hold_summary}\n\n"
            f"{wl_summary}"
        )

    elif text == "/positions":
        import hold_manager
        import watchlist as wl
        reply(monitor.get_open_positions_summary() + "\n\n" + hold_manager.get_long_hold_summary())

    elif text == "/watchlist":
        import watchlist as wl
        reply(wl.get_watchlist_summary())

    elif text == "/pause":
        bot_ref["paused"] = True
        reply("⏸ Bot PAUSED — no new buys. Use /resume to continue.")

    elif text == "/resume":
        bot_ref["paused"] = False
        reply("▶️ Bot RESUMED — scanning for new coins.")

    elif text == "/sellall":
        import trader, wallet as w
        reply("⚠️ Selling all positions...")
        for mint, pos in list(monitor.positions.items()):
            result = trader.sell_token(mint)
            send_sell_result(
                mint=mint,
                ticker=pos.get("ticker", "?"),
                name=pos.get("name", "?"),
                tx_result=result,
                reason="Manual /sellall",
            )
            if result["success"]:
                monitor.remove_position(mint)

    elif text == "/clearmanual":
        import hold_manager
        cleared = [
            mint for mint, pos in list(monitor.positions.items())
            if pos.get("manual")
        ]
        cleared_lh = [
            mint for mint, pos in list(hold_manager.hold_positions.items())
            if pos.get("manual")
        ]
        total = len(cleared) + len(cleared_lh)
        if not total:
            reply("✅ No manual positions to clear.")
        else:
            for mint in cleared:
                monitor.remove_position(mint)
            for mint in cleared_lh:
                hold_manager.remove_hold_position(mint)
            reply(f"🗑 Cleared {total} manual position(s) from tracking.\nMake sure you've already sold them on Jupiter!")

    elif text == "/balance":
        import wallet as w
        import trader as t
        sol = w.get_sol_balance()
        price = t.get_sol_price_usd()
        reply(f"💰 Balance: {sol:.4f} SOL (${sol*price:.2f})")

    elif text.startswith("/wbuy"):
        import watchlist as wl
        import trader as t
        parts = text.split()
        if len(parts) < 2:
            reply("Usage: /wbuy TICKER  (e.g. /wbuy JOTCHUA)")
        else:
            ticker = parts[1].upper().lstrip("$")
            mint   = wl.find_mint_by_ticker(ticker)
            if not mint:
                reply(
                    f"❌ ${ticker} not on watchlist.\n"
                    f"Use /watchlist to see current watchlist."
                )
            else:
                entry = wl.trigger_manual_buy(mint)
                if not entry:
                    reply(f"❌ Could not find ${ticker} on watchlist.")
                else:
                    reply(f"⏳ Manually buying watchlist coin ${ticker}...")
                    tx_result = t.buy_token(mint)
                    if tx_result["success"]:
                        import wallet as w
                        decimals     = t._get_token_decimals(mint)
                        token_amount = tx_result["amount_out"] / (10 ** decimals)
                        sol_price    = t.get_sol_price_usd()
                        sol_spent    = config.BUY_AMOUNT_USD / sol_price
                        entry_price  = (sol_spent / token_amount) * sol_price if token_amount > 0 else 0
                        monitor.add_position(
                            mint=mint,
                            entry_price=entry_price,
                            token_amount=token_amount,
                            score=entry.get("last_score", 0),
                            hold = True,
                            name=entry["name"],
                            ticker=entry["ticker"],
                        )
                        from scanner import mark_token_seen
                        mark_token_seen(mint)
                        wl.remove_from_watchlist(mint)
                        reply(
                            f"✅ Bought ${ticker} from watchlist!\n"
                            f"TX: https://solscan.io/tx/{tx_result['tx']}"
                        )
                    else:
                        reply(f"❌ Buy failed for ${ticker}: {tx_result['error']}")

    elif text.startswith("/holdsell"):
        import hold_manager
        parts = text.split()
        if len(parts) < 2:
            reply("Usage: /holdsell TICKER  (e.g. /holdsell BONK)")
        else:
            ticker = parts[1].upper().lstrip("$")
            mint   = hold_manager.find_mint_by_ticker(ticker)
            if not mint:
                reply(
                    f"❌ No long-hold position found for ${ticker}.\n"
                    f"Use /positions to see current long-hold positions."
                )
            else:
                reply(f"⏳ Selling long-hold position ${ticker}...")
                hold_manager.manual_sell(mint)

    elif text in ["/stats", "/stats7", "/stats30"]:
        import trade_logger
        days = 7 if text == "/stats7" else 30
        reply(trade_logger.format_stats_message(days))

    elif text == "/help":
        reply(
            "📋 <b>Commands:</b>\n"
            "/status — bot status + all positions\n"
            "/positions — open + long-hold positions\n"
            "/balance — wallet balance\n"
            "/stats — PnL stats last 30 days\n"
            "/stats7 — PnL stats last 7 days\n"
            "/pause — stop new buys\n"
            "/resume — restart buying\n"
            "/sellall — emergency sell all active positions\n"
            "/holdsell TICKER — manually sell a long-hold position\n"
            "/watchlist — show watchlist coins\n"
            "/wbuy TICKER — manually buy a watchlist coin\n"
            "/clearmanual — remove manually-sold stuck positions\n"
            "/help — this message"
        )
    else:
        reply(f"Unknown command: {text}\nUse /help")
