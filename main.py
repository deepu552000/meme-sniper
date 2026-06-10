#!/usr/bin/env python3
# main.py — Meme Sniper Bot entry point

import time
import json
import traceback
import config
import scanner
import scorer
import trader
import monitor
import hold_manager
import watchlist as wl
import telegram_bot as tg
import wallet as w

# Shared state dict passed to command listener
bot_state = {"paused": False}


def _save_score_log(token: dict, result: dict, score: int):
    """Save all scored tokens to score_log.json.
    Under 15 → minimal entry (name, ticker, score only).
    15 and above → full breakdown with liq, holders, volume etc.
    """
    try:
        entry = {
            "time":   time.strftime("%Y-%m-%d %H:%M:%S"),
            "mint":   token.get("mint", ""),
            "name":   token.get("name", ""),
            "ticker": token.get("ticker", ""),
            "score":  score,
        }
        if score >= 15 or result.get("reject_reason"):
            bd = result.get("breakdown", {})
            entry.update({
                "buy":           result["buy"],
                "hold":          result["hold"],
                "reject_reason": result.get("reject_reason"),
                "breakdown":     bd,
            })
        with open("score_log.json", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[main] Could not save score log: {e}")


def main():
    print("=" * 50)
    print("  MEME SNIPER BOT")
    print("=" * 50)

    # Sanity checks
    if "YOUR_" in config.PRIVATE_KEY:
        print("❌ ERROR: Set your PRIVATE_KEY in config.py first!")
        return
    if "YOUR_" in config.TELEGRAM_TOKEN:
        print("❌ ERROR: Set your TELEGRAM_TOKEN in config.py first!")
        return

    # Check wallet balance
    sol_bal = w.get_sol_balance()
    sol_price = trader.get_sol_price_usd()
    usd_bal = sol_bal * sol_price
    print(f"Wallet: {w.get_pubkey()}")
    print(f"Balance: {sol_bal:.4f} SOL (${usd_bal:.2f})")

    if usd_bal < config.BUY_AMOUNT_USD:
        print(f"❌ Insufficient balance! Need at least ${config.BUY_AMOUNT_USD}")
        return

    # Start Telegram + wire sell-failure alerts
    trader.setup_alerts()
    tg.send_startup()
    tg.start_command_listener(bot_state)
    print(f"Telegram alerts active → chat {config.TELEGRAM_CHAT_ID}")

    print(f"\nScanning every {config.SCAN_INTERVAL_SEC}s | Min score: {config.MIN_SCORE_TO_BUY}")
    print("Press Ctrl+C to stop\n")

    scan_count              = 0
    last_monitor_time       = 0
    last_fast_monitor_time  = 0   # Fix 1: fast-poll young positions
    last_long_hold_time     = 0   # Long-hold tier: check every 5 min
    last_watchlist_time     = 0   # Watchlist: check every 30 min
    last_heartbeat_time     = time.time()
    HEARTBEAT_INTERVAL      = 6 * 3600   # 6 hours
    FAST_MONITOR_INTERVAL   = 5          # Fix 1: check young positions every 5s

    while True:
        try:
            # ── Heartbeat (every 6 hours — all channels) ──────────────
            if time.time() - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                tg.send_heartbeat(scan_count, bot_state)
                last_heartbeat_time = time.time()

            # ── Fast-poll young positions every 5s (Fix 1) ───────────────
            if time.time() - last_fast_monitor_time >= FAST_MONITOR_INTERVAL:
                monitor.check_young_positions()
                last_fast_monitor_time = time.time()

            # ── Monitor open positions (every MONITOR_INTERVAL_SEC) ──────
            if time.time() - last_monitor_time >= config.MONITOR_INTERVAL_SEC:
                monitor.check_positions()
                last_monitor_time = time.time()

            # ── Monitor long-hold positions (every 5 min) ─────────────────
            if time.time() - last_long_hold_time >= hold_manager.LONG_HOLD_MONITOR_SEC:
                hold_manager.check_long_holds()
                last_long_hold_time = time.time()

            # ── Watchlist check (every 5 min) ────────────────────────────────
            if time.time() - last_watchlist_time >= wl.WATCHLIST_CHECK_SEC:
                if not bot_state["paused"] and len(monitor.positions) < config.MAX_POSITIONS:
                    mints_to_buy = wl.check_watchlist(bot_state)
                    for mint in mints_to_buy:
                        if len(monitor.positions) >= config.MAX_POSITIONS:
                            break
                        entry = wl.watchlist.get(mint)
                        if not entry:
                            continue
                        sol_bal   = w.get_sol_balance()
                        sol_price = trader.get_sol_price_usd()
                        if sol_bal * sol_price < config.BUY_AMOUNT_USD:
                            tg.send("⚠️ Insufficient SOL balance — skipping watchlist buy")
                            break
                        tx_result = trader.buy_token(mint)
                        if tx_result["success"]:
                            from scanner import mark_token_seen
                            mark_token_seen(mint)
                            decimals     = trader._get_token_decimals(mint)
                            token_amount = tx_result["amount_out"] / (10 ** decimals)
                            sol_price_now = trader.get_sol_price_usd()
                            # Use actual lamports spent if available (more accurate)
                            if tx_result.get("sol_spent_lamports"):
                                sol_spent = tx_result["sol_spent_lamports"] / 1_000_000_000
                            else:
                                sol_spent = config.BUY_AMOUNT_USD / sol_price_now
                            entry_price  = (sol_spent / token_amount) * sol_price_now if token_amount > 0 else 0
                            monitor.add_position(
                                mint=mint,
                                entry_price=entry_price,
                                token_amount=token_amount,
                                score=entry.get("last_score", 0),
                                hold=True,
                                name=entry["name"],
                                ticker=entry["ticker"],
                            )
                            wl.remove_from_watchlist(mint)
                            # Send buy confirmation with Solscan link like normal buys
                            tg.send(
                                f"✅ <b>WATCHLIST BUY CONFIRMED</b>\n"
                                f"Token: {entry['name']} (${entry['ticker']})\n"
                                f"Entry: ${entry_price:.8f}\n"
                                f"Mode: HOLD 🫂 (watchlist buy)\n"
                                f"TX: <a href='https://solscan.io/tx/{tx_result['tx']}'>View on Solscan</a>"
                            )
                        else:
                            tg.send(f"❌ Watchlist buy failed for ${entry['ticker']}: {tx_result['error']}")
                last_watchlist_time = time.time()

            # ── Scan for new coins ────────────────────────────────────
            if not bot_state["paused"]:
                scan_count += 1
                print(f"\n[{time.strftime('%H:%M:%S')}] Scan #{scan_count} | "
                      f"Open positions: {len(monitor.positions)}/{config.MAX_POSITIONS}")

                if len(monitor.positions) >= config.MAX_POSITIONS:
                    print("  Max positions reached, skipping new buys")
                else:
                    tokens = scanner.get_new_tokens()
                    print(f"  Found {len(tokens)} new tokens to evaluate")

                    for token in tokens:
                        # Stop if we hit max positions mid-loop
                        if len(monitor.positions) >= config.MAX_POSITIONS:
                            break

                        name   = token.get("name", "?")
                        ticker = token.get("ticker", "?")
                        mint   = token.get("mint", "")

                        print(f"  Scoring: {name} (${ticker})...", end=" ")

                        result = scorer.score_token(token)
                        score  = result["score"]

                        # Hard reject
                        if result["reject_reason"]:
                            _save_score_log(token, result, 0)

                            # result["requeue"]=True  → scorer flagged this for recheck
                            #   (whale 25-40%, missing RugCheck data, holders/volume too low)
                            # result["requeue"]=False → permanent blacklist
                            should_requeue = result.get("requeue", False) or any(
                                kw in result["reject_reason"].lower()
                                for kw in ("holders", "volume", "token too new")
                            )
                            if should_requeue:
                                from scanner import _add_pending
                                _add_pending(token)
                                print(f"REJECTED (requeued) — {result['reject_reason']}")
                            else:
                                # Permanent reject — blacklist so we never see it again
                                from scanner import mark_token_seen
                                mark_token_seen(mint)
                                print(f"REJECTED — {result['reject_reason']}")
                            continue

                        print(f"score={score}")

                        # Print score breakdown for scores worth knowing about
                        if score >= 25:
                            bd = result.get("breakdown", {})
                            print(f"    onchain={bd.get('onchain_total',0)} momentum={bd.get('momentum_total',0)} "
                                  f"liq={bd.get('liquidity','?')} lp={bd.get('lp_locked','?')} "
                                  f"dev={bd.get('dev_wallet','?')} top1={bd.get('top_holder','?')} top10={bd.get('top10','?')} "
                                  f"holders={bd.get('holders','?')} buysell={bd.get('buy_sell_ratio','?')} "
                                  f"vol5m={bd.get('volume_5m','?')} age={bd.get('age_penalty','?')} "
                                  f"twitter={bd.get('twitter','?')} web={bd.get('website','?')}")

                        # ── Save to score_log.json (all tokens) ─────────
                        _save_score_log(token, result, score)

                        # Notify on any coin we are about to buy (score >= MIN_SCORE_TO_BUY)
                        if score >= config.MIN_SCORE_TO_BUY:
                            tg.send_scan_found(token, result)

                        # If score is close but not there yet — requeue for recheck
                        # AND add to watchlist for long-term monitoring
                        if not result["buy"] and score >= 25 and token.get("age_minutes", 999) < 60:
                            from scanner import _add_pending
                            _add_pending(token)
                            wl.add_to_watchlist(token, score)
                            print(f"  score={score} (below threshold, requeued + watchlisted)")
                            continue

                        # Too low score and not worth rechecking — blacklist
                        if not result["buy"]:
                            from scanner import mark_token_seen
                            mark_token_seen(mint)
                            print(f"  score={score} (too low, skipping permanently)")
                            continue

                        # Buy decision
                        if result["buy"]:
                            # Check we still have budget (refetch sol_price — may have moved)
                            sol_bal = w.get_sol_balance()
                            sol_price = trader.get_sol_price_usd()
                            if sol_bal * sol_price < config.BUY_AMOUNT_USD:
                                tg.send("⚠️ Insufficient SOL balance — pausing buys")
                                bot_state["paused"] = True
                                break

                            tx_result = trader.buy_token(mint)
                            tg.send_buy_result(token, result, tx_result)

                            if tx_result["success"]:
                                # Mark seen only on successful buy — failed buys can retry next cycle
                                from scanner import mark_token_seen
                                mark_token_seen(mint)

                                # Calculate exact entry price from actual execution
                                # amount_out = tokens received, BUY_AMOUNT_USD = SOL spent
                                # Avoids DexScreener 3s delay inflating entry on fast movers
                                decimals = trader._get_token_decimals(mint)
                                token_amount = tx_result["amount_out"] / (10 ** decimals)
                                sol_price_now = trader.get_sol_price_usd()
                                # Use actual lamports spent if available (more accurate than BUY_AMOUNT_USD)
                                if tx_result.get("sol_spent_lamports"):
                                    sol_spent = tx_result["sol_spent_lamports"] / 1_000_000_000
                                else:
                                    sol_spent = config.BUY_AMOUNT_USD / sol_price_now
                                entry_price = (sol_spent / token_amount) * sol_price_now if token_amount > 0 else 0

                                # Fallback to DexScreener if calculation failed
                                if not entry_price:
                                    time.sleep(3)
                                    from scanner import _get_dexscreener_token
                                    dex = _get_dexscreener_token(mint)
                                    entry_price = dex.get("price_usd", 0) or token.get("price_usd", 0)

                                monitor.add_position(
                                    mint=mint,
                                    entry_price=entry_price,
                                    token_amount=token_amount,
                                    score=score,
                                    hold=result["hold"],
                                    name=name,
                                    ticker=ticker,
                                )

            # ── Sleep ──────────────────────────────────────────────────
            time.sleep(config.SCAN_INTERVAL_SEC)

        except KeyboardInterrupt:
            print("\n\nStopping bot...")
            tg.send("🛑 Meme Sniper Bot stopped manually.")
            break

        except Exception as e:
            err_msg = f"[main] Unexpected error: {e}"
            print(err_msg)
            traceback.print_exc()
            tg.send(f"⚠️ Bot error: {e}\nResuming in 30s...")
            time.sleep(30)


if __name__ == "__main__":
    main()
