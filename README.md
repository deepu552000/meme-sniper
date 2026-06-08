# 🚀 Meme Sniper Bot

Automated Solana meme coin sniper with Twitter/website safety scoring, auto-buy, and take-profit/stop-loss.

---

## Files

| File | Purpose |
|---|---|
| `config.py` | All your settings — **edit this first** |
| `main.py` | Entry point — run this |
| `scanner.py` | Scans pump.fun + DexScreener for new coins |
| `scorer.py` | Scores each coin 0–100 for safety |
| `twitter_web.py` | Twitter activity + website content analysis |
| `trader.py` | Buys/sells via Jupiter Aggregator |
| `monitor.py` | Watches open positions, triggers TP/SL |
| `telegram_bot.py` | Sends alerts, handles commands |
| `wallet.py` | Solana wallet helpers |

---

## Quick Start

```bash
# 1. Upload files to your VPS
scp -r meme-sniper/ user@your-vps-ip:~/

# 2. SSH in and run setup
ssh user@your-vps-ip
cd meme-sniper
bash setup.sh

# 3. Edit config
nano config.py
# Fill in PRIVATE_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# 4. Activate venv and run
source venv/bin/activate
python3 main.py
```

---

## Scoring System (0–100)

### On-chain safety (35 pts max)
| Check | Points |
|---|---|
| Liquidity $100k+ | 12 |
| Liquidity $50k+ | 9 |
| Liquidity $20k+ | 6 |
| LP locked 80%+ | 10 |
| LP locked 50%+ | 6 |
| Dev wallet ≤2% | 8 |
| Dev wallet ≤5% | 5 |
| Top 10 wallets ≤30% supply | 5 |

### Momentum (20 pts max)
| Check | Points |
|---|---|
| 500+ holders | 8 |
| 70%+ buy/sell ratio | 7 |
| $50k+ 5m volume | 5 |

### Twitter (40 pts max)
| Check | Points |
|---|---|
| 10k+ followers | 15 |
| 2k+ followers | 10 |
| 100+ tweets | 8 |
| Account since 2022 or earlier | 10 |
| Recently active | 7 |
| Pre-launch Twitter presence | +15 bonus |

### Website (20 pts max)
| Check | Points |
|---|---|
| Website live | 5 |
| Rich content (6+ keywords) | 8 |
| 2+ social links | 4 |
| .com/.net/.org domain | 3 |

### Hard rejects (instant skip, no score)
- ❌ Mint authority not revoked
- ❌ Freeze authority not revoked
- ❌ Dev holds >15%
- ❌ Liquidity < $5,000
- ❌ Fewer than 20 holders
- ❌ Top 10 wallets hold >80%

---

## Sell Conditions

| Condition | Action |
|---|---|
| -10% from entry | Stop loss — sell immediately |
| +10% from entry (quick flip) | Take profit — sell |
| +30% from entry (hold mode) | Take profit — sell |
| 48h elapsed in hold mode | Exit if in profit |
| -20% from peak in <1h | Dump detected — sell |
| 85%+ sell txns in 1 min | Whale dump — sell |
| -25% price in 1 min | Flash crash — sell |

---

## Telegram Commands

| Command | Action |
|---|---|
| `/status` | Bot status + open positions |
| `/positions` | All open trades with PnL |
| `/balance` | Current SOL balance |
| `/pause` | Stop new buys (keeps monitoring) |
| `/resume` | Resume buying |
| `/sellall` | Emergency: sell everything now |
| `/help` | Command list |

---

## Recommended RPC

The free public Solana RPC (`api.mainnet-beta.solana.com`) can be slow/rate-limited.
For better reliability, get a **free** Helius key at https://helius.dev and set:

```python
RPC_URL = "https://mainnet.helius-rpc.com/?api-key=YOUR_FREE_KEY"
```

---

## Safety Tips

1. **Start with $15** — 3 × $5 positions max
2. **Never add more than you can lose** — meme coins are extremely high risk
3. **Watch the first few buys manually** — make sure it's buying/selling correctly
4. **Check `/balance` often** — make sure SOL is enough for gas fees (~0.001 SOL per tx)
5. **Use `/pause` if anything looks wrong** — stops new buys instantly
6. **Keep some SOL for fees** — leave at least 0.05 SOL above your trading budget

---

## How Pre-launch Twitter Detection Works

The bot searches DuckDuckGo for the coin's ticker and name before the contract was promoted. If it finds Twitter mentions that pre-date the launch push, it adds a +15 score bonus. These "community-first" coins tend to have more organic holders and less bot activity.

---

## Disclaimer

This bot trades real money. Meme coins are extremely volatile. You can lose 100% of what you put in. This is a learning/experiment tool — trade only what you're fully prepared to lose.
