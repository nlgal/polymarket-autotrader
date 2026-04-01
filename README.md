# Polymarket Auto-Trader

Autonomous prediction market trading bot running on DigitalOcean (Amsterdam).  
Wallet: `0xc2c1892653C175113c65961C7F4227c18D09b52a`

## Architecture

```
autotrader.py          15-min loop — scores markets, places/manages positions
opportunity_scanner.py 4h scan — finds new opportunities with bull/bear debate
live_sports_trader.py  Snowball strategy for tournament markets
lp_quoter.py           Hourly LP reward quoter (two-sided limit orders)
discord_monitor.py     Monitors dkxbt #predictions, #member-calls, #sports
whale_monitor.py       Hourly check of whale watchlist for $500+ moves
whale_scanner.py       Leaderboard scanner — Brier Score filter, 100k+ PnL
auto_redeem.py         Redeems resolved winning positions via CTF contract
strategy_optimizer.py  Daily Karpathy Loop — reviews PnL and proposes config changes
executor.py            HMAC-signed HTTP executor service (port 8888)
```

## Key Scripts

| Script | Purpose |
|--------|---------|
| `autotrader.py` | Main trading loop. Kelly sizing, UW signals, fast-reject gate, token counter |
| `opportunity_scanner.py` | 4h scanner. Bull/bear debate, consensus vote, scan score cache |
| `lp_quoter.py` | LP reward farming. Two-sided GTC orders, fill tracking, kill switch |
| `merge_apr30.py` | One-time: merge ceasefire Apr30 YES+NO strangle → USDC |
| `preflight.py` | Pre-flight checks before deployment |
| `rederive_and_sell.py` | Full deploy: fetches all scripts via GitHub API, clears pycache, restarts service |

## Safety Rules

- `HARD_RULES.md` — machine-readable guardrails (Lesson 12: never sell live sports markets)
- `sports_blacklist.json` — runtime JSON blacklist bypasses .pyc cache
- `NEAR_RESOLUTION_THRESHOLD = 0.99` — holds positions until near-certain
- No on-chain transactions without explicit approval

## Token Cost Tracking

Both `autotrader.py` and `opportunity_scanner.py` log token usage per run:
```
[TOKENS] 4 calls | in=2,840 out=620 cache_rd=1,200 | est cost $0.0047 | daily@96cyc ~$0.451/day
```
Haiku 4.5 pricing: $0.80/M input · $4.00/M output · $0.08/M cache-read

## Cron Schedule (optimized 2026-03-31)

| Cron | Frequency | Purpose |
|------|-----------|---------|
| Opportunity Scanner | every 4h | Find new trades |
| Strategy Optimizer | daily midnight | Tune config |
| Health Monitor | every 4h | Error/dead-bot detection |
| Sports Trader | every 4h | Tournament snowball |
| Discord Monitor | every 2h | dkxbt calls |
| Whale Monitor | every 2h | $500+ whale moves |
| LP Quoter | every 4h | Maintain resting LP orders |
| Whale Refresh | weekly Mon | Rotate stale wallets |

## Intelligence Files

```
intelligence/soul.md      Core trading principles
intelligence/lessons.md   Auto-generated from resolved trades
intelligence/playbook.md  Market-specific playbooks
CLAUDE.md                 Persistent context for scoring agent
HARD_RULES.md             Hard guardrails (override everything)
```

## Server

- IP: `167.71.68.143` (DigitalOcean Amsterdam)
- Service: `polymarket.service` (autotrader), `executor.service` (port 8888)
- Agent path: `/opt/polymarket-agent/`
