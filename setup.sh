#!/bin/bash
# setup.sh — One-time setup for Meme Sniper Bot on Ubuntu VPS
# Run: bash setup.sh

set -e
echo "================================================"
echo "  MEME SNIPER BOT - VPS SETUP"
echo "================================================"

# System deps
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv screen git

# Python venv
echo "[2/5] Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Python packages
echo "[3/5] Installing Python packages..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "[4/5] Done installing packages."

# Check config
if grep -q "YOUR_SOLANA_WALLET" config.py; then
    echo ""
    echo "⚠️  IMPORTANT: Edit config.py before running!"
    echo "   nano config.py"
    echo ""
    echo "   You need to set:"
    echo "   - PRIVATE_KEY    (your Solana wallet private key)"
    echo "   - TELEGRAM_TOKEN (your bot token from @BotFather)"
    echo "   - TELEGRAM_CHAT_ID (your chat ID from @userinfobot)"
fi

echo "[5/5] Setup complete!"
echo ""
echo "================================================"
echo "  HOW TO RUN"
echo "================================================"
echo ""
echo "  # Activate venv first (always do this):"
echo "  source venv/bin/activate"
echo ""
echo "  # Test run (see output):"
echo "  python3 main.py"
echo ""
echo "  # Run in background (recommended for VPS):"
echo "  screen -S sniper"
echo "  source venv/bin/activate"
echo "  python3 main.py"
echo "  # Detach: Ctrl+A then D"
echo "  # Re-attach: screen -r sniper"
echo ""
echo "  # Or use nohup:"
echo "  nohup python3 main.py > sniper.log 2>&1 &"
echo "  tail -f sniper.log"
echo ""
echo "  Telegram commands once running:"
echo "  /status  /positions  /balance  /pause  /resume  /sellall  /help"
