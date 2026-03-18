#!/bin/bash
# ── Update the agent without losing .env or state ─────────────────────────────
# Run from the repo directory: bash scripts/update.sh
# ──────────────────────────────────────────────────────────────────────────────

AGENT_DIR="/opt/polymarket-agent"

echo "→ Backing up state..."
cp "$AGENT_DIR/.env" /tmp/pm_env_backup 2>/dev/null && echo "  .env backed up"
cp "$AGENT_DIR/state.json" /tmp/pm_state_backup 2>/dev/null && echo "  state.json backed up"

echo "→ Updating agent files..."
cp autotrader.py "$AGENT_DIR/"
cp requirements.txt "$AGENT_DIR/"
cp -r intelligence/ "$AGENT_DIR/"

echo "→ Restoring state..."
cp /tmp/pm_env_backup "$AGENT_DIR/.env" 2>/dev/null && echo "  .env restored"
cp /tmp/pm_state_backup "$AGENT_DIR/state.json" 2>/dev/null && echo "  state.json restored"

echo "→ Installing any new dependencies..."
/opt/polymarket-agent/venv/bin/pip install --quiet -r "$AGENT_DIR/requirements.txt"

echo "→ Restarting agent..."
systemctl restart polymarket

sleep 3
echo ""
echo "✓ Update complete. Recent logs:"
tail -10 /var/log/polymarket/autotrader.log
