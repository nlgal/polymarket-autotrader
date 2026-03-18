# Polymarket Auto-Trader

An autonomous prediction market trading agent that uses a 3-persona AI swarm consensus system, smart money signals from Unusual Whales, and real-time news arbitrage to find and execute trades on [Polymarket](https://polymarket.com).

> ⚠️ **This is experimental software that trades real money. Use at your own risk. Never invest more than you can afford to lose.**

---

## Features

- **3-Persona Swarm Scoring** — Each market is scored independently by Bull, Bear, and Neutral AI analysts. Trades only execute when ≥2/3 agree (inspired by [BuBBliK's $1.49M swarm model](https://x.com/k1rallik))
- **Smart Money Signals** — Integrates Unusual Whales API for insider flow, smart money gap, and unusual activity detection
- **News Arbitrage** — Perplexity-powered real-time headline scanning to catch market mispricings before the crowd
- **Adaptive Risk Modes** — NORMAL / RECOVERY / EXPANSION / PAUSED based on drawdown from equity peak
- **Sports Market Filters** — Higher edge threshold (15%), price floor (15¢), and size cap ($30) for volatile sports markets
- **Auto-Sell with Allowances** — Pre-approves conditional token allowances at buy time for seamless exits
- **Duplicate Sell Protection** — Checks open orders before re-selling to prevent repeated stop-loss fires
- **Telegram Notifications** — Silent cycle summaries + loud alerts for trades and hard pauses
- **Self-Improvement** — Logs mistakes and lessons in `/intelligence/` for ongoing prompt improvement

---

## How It Works

```
Every 15 minutes:
  1. Fetch top 60 markets by 24h volume
  2. Score top 20 using 3-persona swarm (Bull/Bear/Neutral via Claude Haiku)
  3. Inject Unusual Whales smart money signals
  4. Only trade on 2/3 consensus with edge > 7% and spread < 20pp
  5. Apply sports filters, per-market caps, and mode-based sizing
  6. Manage existing positions (profit targets, stop losses, near-resolution exits)
  7. Send Telegram summary

Every 5 minutes (between full scans):
  - News arbitrage scan via Perplexity
```

### Trading Modes

| Mode | Trigger | Trade Size | Max Orders |
|------|---------|------------|------------|
| NORMAL | Default | $40–150 | 8 |
| RECOVERY | Drawdown ≥ 10% | $25–75 | 4 |
| EXPANSION | New peak ≥ +10% | $75–200 | 10 |
| PAUSED | Drawdown ≥ 20% or daily hard stop | None | 0 |

---

## Requirements

- Python 3.10+
- Ubuntu 22.04 server (DigitalOcean, AWS, etc.) — 1 vCPU / 1GB RAM is fine
- API keys (see below)

### Required API Keys

| Service | Purpose | Get It |
|---------|---------|--------|
| [Anthropic](https://console.anthropic.com/) | Claude Haiku for market scoring | Free tier available |
| [Perplexity](https://www.perplexity.ai/settings/api) | Real-time news context | Paid |
| Polymarket wallet | Sign and fund trades | See wallet setup below |

### Optional API Keys

| Service | Purpose | Get It |
|---------|---------|--------|
| [Unusual Whales](https://unusualwhales.com/api) | Smart money signals | Paid |
| Telegram Bot | Trade notifications | Free via [@BotFather](https://t.me/BotFather) |

---

## Wallet Setup

Polymarket uses a **Gnosis Safe** proxy wallet. You need two addresses:

1. **Funder/Proxy address** — shown on your Polymarket profile page. This is where USDC lives.
2. **Signer private key** — the EOA that signs transactions for the Safe.

To find your signer key:
1. Go to [polymarket.com](https://polymarket.com) and connect your wallet
2. The signer is the embedded wallet created by Polymarket (via Magic/Privy)
3. Export it from your wallet provider's settings

> ⚠️ Never share your private key. Store it only in your `.env` file, never in code.

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/polymarket-autotrader.git
cd polymarket-autotrader
```

### 2. Configure your API keys

```bash
cp .env.example .env
nano .env   # Fill in all your keys
```

### 3. Run the setup script (on your server)

```bash
bash scripts/setup_server.sh
```

### 4. Edit the .env on the server

```bash
nano /opt/polymarket-agent/.env
```

### 5. Start the agent

```bash
systemctl start polymarket
tail -f /var/log/polymarket/autotrader.log
```

---

## Configuration

All key parameters are at the top of `autotrader.py`:

```python
# Edge thresholds
MIN_EDGE         = 0.07   # Minimum edge to consider a trade (7%)
SPORTS_MIN_EDGE  = 0.15   # Higher bar for sports markets (15%)
SPORTS_MIN_PRICE = 0.15   # Skip sports tokens below 15¢ (in-play protection)
SPORTS_MAX_SIZE  = 30     # Maximum size for sports trades ($30)

# Risk limits
DD_RECOVERY      = 0.10   # Drawdown threshold to enter RECOVERY mode (10%)
DD_HARD_PAUSE    = 0.20   # Drawdown threshold to pause all trading (20%)
DAILY_SOFT_STOP  = 0.05   # Daily loss limit before closing-only mode (5%)
DAILY_HARD_STOP  = 0.08   # Daily loss limit before full pause (8%)
MAX_PER_MARKET_USDC = 200 # Maximum exposure per market ($200)

# Position management
PROFIT_TARGET    = 0.88   # Sell when token reaches 88¢
STOP_LOSS        = 0.50   # Sell when position loses 50% of entry value
```

---

## Updating

To update without losing your `.env` or portfolio state:

```bash
git pull
bash scripts/update.sh
```

---

## Monitoring

```bash
# Live logs
tail -f /var/log/polymarket/autotrader.log

# Service status
systemctl status polymarket

# Check current portfolio via API
python3 -c "
import requests
funder = 'YOUR_FUNDER_ADDRESS'
r = requests.get('https://data-api.polymarket.com/positions', params={'user': funder, 'sizeThreshold': '0.01'})
for p in r.json():
    print(f\"\${p.get('currentValue',0):.2f} | {p.get('outcome')} | {p.get('title','')[:60]}\")
"
```

---

## Architecture

```
autotrader.py          Main agent loop
├── score_market()     3-persona swarm scoring (Bull/Bear/Neutral)
├── manage_positions() Stop loss / profit target / near-resolution exits
├── place_trade()      Order execution with pre-approved allowances
├── news_arb_scan()    Perplexity news arbitrage scanner
├── fetch_uw_signals() Unusual Whales smart money data
└── update_control_plane() Risk mode management

intelligence/
├── soul.md            Core trading principles (injected into every prompt)
├── lessons.md         Learned lessons from past cycles
├── mistakes.md        Known failure modes to avoid
└── playbook.md        Market selection and sizing strategy
```

---

## Disclaimer

This software is provided as-is with no warranty. Prediction market trading involves significant financial risk. Past performance does not guarantee future results. The swarm scoring system, Unusual Whales integration, and all other features are experimental. Always start with `DRY_RUN=true` to verify the agent is working before trading real money.
