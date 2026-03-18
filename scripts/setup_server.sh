#!/bin/bash
# ── Polymarket Auto-Trader Server Setup ───────────────────────────────────────
# Tested on Ubuntu 22.04 (DigitalOcean, AWS, etc.)
# Run as root: bash setup_server.sh
# ──────────────────────────────────────────────────────────────────────────────

set -e

AGENT_DIR="/opt/polymarket-agent"
VENV="$AGENT_DIR/venv"
LOG_DIR="/var/log/polymarket"
SERVICE="polymarket"

echo "→ Installing system dependencies..."
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git

echo "→ Creating agent directory..."
mkdir -p "$AGENT_DIR"
mkdir -p "$LOG_DIR"
mkdir -p "$AGENT_DIR/intelligence"

echo "→ Copying agent files..."
cp autotrader.py "$AGENT_DIR/"
cp requirements.txt "$AGENT_DIR/"
cp -r intelligence/ "$AGENT_DIR/"

echo "→ Setting up Python virtual environment..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$AGENT_DIR/requirements.txt"

echo "→ Setting up .env..."
if [ ! -f "$AGENT_DIR/.env" ]; then
    cp .env.example "$AGENT_DIR/.env"
    echo ""
    echo "⚠️  IMPORTANT: Edit $AGENT_DIR/.env with your API keys before starting:"
    echo "   nano $AGENT_DIR/.env"
    echo ""
fi

echo "→ Creating systemd service..."
cat > /etc/systemd/system/polymarket.service << SERVICE
[Unit]
Description=Polymarket Auto-Trader
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=root
WorkingDirectory=$AGENT_DIR
ExecStart=$VENV/bin/python3 $AGENT_DIR/autotrader.py
Restart=always
RestartSec=10
StandardOutput=append:$LOG_DIR/autotrader.log
StandardError=append:$LOG_DIR/autotrader.log
EnvironmentFile=$AGENT_DIR/.env

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable polymarket

echo ""
echo "✓ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit your .env:  nano $AGENT_DIR/.env"
echo "  2. Start the agent: systemctl start polymarket"
echo "  3. View logs:       tail -f $LOG_DIR/autotrader.log"
echo "  4. Check status:    systemctl status polymarket"
