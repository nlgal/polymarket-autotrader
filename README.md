# Polymarket Auto-Trader

Autonomous prediction market trading agent running on DigitalOcean (Amsterdam).  
Wallet: `0xc2c1892653C175113c65961C7F4227c18D09b52a`  
Current version: **v2.3.0** — see [OPERATING_CONSTITUTION.md](OPERATING_CONSTITUTION.md) for full system spec.

## Architecture

```
autotrader.py          15-min loop — scores markets, places/manages positions
opportunity_scanner.py 4h scan — finds new opportunities with bull/bear debate
lp_quoter.py           Hourly LP reward quoter (two-sided GTD limit orders)
live_sports_trader.py  Snowball strategy for tournament markets
discord_monitor.py     Monitors dkxbt #predictions, #member-calls, #sports
whale_monitor.py       Hourly check of whale watchlist for $500+ moves
whale_scanner.py       Leaderboard scanner — Brier Score filter, 100k+ PnL
auto_redeem.py         Redeems resolved winning positions via CTF contract
strategy_optimizer.py  Daily Karpathy Loop — reviews PnL, proposes config changes
market_guardrails.py   Protective risk-off rules (Hungary NO guardrail, etc.)
post_trade_review.py   JSONL failure-mode logger + repeated-loss detection
position_monitor.py    Per-position P&L, expiry, whale linkage, news alerts
executor.py            HMAC-signed HTTP executor service (port 8888)
```

## Key Files

| File | Purpose |
|------|---------|
| `OPERATING_CONSTITUTION.md` | Permanent system specification — 10-step decision hierarchy, 12 hard rules, objective function. Injected into every optimizer run. |
| `HARD_RULES.md` | Machine-readable guardrails synced from code |
| `CLAUDE.md` | Auto-generated nightly — live portfolio snapshot, config values, constitution targets |
| `intelligence/post_trade_reviews.jsonl` | Append-only post-trade log. Failure mode detection fires on 3+ repeats. |

## Safety System (v2.3.0)

Three independent layers before any order reaches the wire:

**Layer 1 — SPORTS_KEYWORDS (380 keywords)**  
Catches fee-market categories at market classification: all major sports leagues, 25+ European soccer clubs, FIFA World Cup nations, esports, box office, finance/macro, crypto tickers, weather.

**Layer 2 — Fee-market price guard in score_market()**  
If `fees_enabled=True` and `YES < 0.20` or `YES > 0.83` → hard PASS before any LLM call.

**Layer 3 — Universal preflight checklist in place_trade()**  
Runs on *every* code path (autotrader, news arb, scanner, resubmit) before any order is placed:
- Check 1: Fee market + extreme price (YES < 0.20 or YES > 0.83) → block
- Check 2: Fee market + not sports + not approved category + no catalyst → block
- Check 3: Sports market BUY_YES/NO at price < 0.25 → block
- Check 4: Match-day "win on YYYY-MM-DD" pattern → always block
- Check 5: Trade price < 0.05 → block

*Man City lesson (Apr 4 2026): YES=0.12 fee-market sports bet slipped through keyword gate into news arb path. Lost $150. All three layers now independently catch this.*

## Priority Watchlist

`opportunity_scanner.py` always evaluates these markets regardless of `yes_p` filter:

| Market | Token (YES) |
|--------|-------------|
| Ceasefire Apr7 | `82855...` |
| Ceasefire Apr15 | `85191...` |
| Ceasefire Apr30 | `44149...` |
| Ceasefire May31 | dynamic lookup |
| Trump ends Iran ops | dynamic lookup |

## LP Configuration

| Market | Mode | Fill limit | Max inventory |
|--------|------|------------|---------------|
| Ceasefire Apr15 | Two-sided | $2,000/side | 1,400sh |
| Ceasefire Apr30 | Two-sided | $2,000/side | 1,000sh |
| Ceasefire Apr7 | YES-only (holding 900 YES) | $2,000 | 1,000sh |

GTD 70-minute orders, 1.5-tick pullback, activity guard, fill scaling.

## Strategy Optimizer (Karpathy Loop)

Runs nightly at midnight UTC. Reads:
- Last 20 closed trades (win/loss, PnL)
- `intelligence/post_trade_reviews.jsonl` (failure mode counts, capital lost per mode)
- `OPERATING_CONSTITUTION.md` (allocation targets, hard rules)

Proposes ONE parameter change. Validates against constitution before simulation. Rejects if score doesn't improve.

## Cron Schedule

| Name | Frequency | Purpose |
|------|-----------|---------|
| Opportunity Scanner | every 4h | Find new directional trades |
| Strategy Optimizer | daily midnight | Tune config via Karpathy Loop |
| Health Monitor | every 4h | Error/dead-bot/contradiction detection |
| LP Quoter | every 4h | Maintain resting LP orders |
| Sports Trader | every 4h | Tournament snowball entries |
| Discord Monitor | every 2h | dkxbt signal ingestion |
| Whale Monitor | every 2h | $500+ wallet moves |
| Whale Watchlist Refresh | weekly Mon | Rotate stale wallets |
| Tooling Watch | weekly Mon | Backtesting repo + PMXT release checks |
| LP Stipend Check | weekly Mon | Alert when equity ≥ $10k for qcex.com |

## Server

- IP: `167.71.68.143` (DigitalOcean Amsterdam)
- Services: `polymarket.service` (autotrader), `executor.service` (port 8888)
- Agent path: `/opt/polymarket-agent/`
- GitHub: `nlgal/polymarket-autotrader`

## Version History

| Version | Date | Change |
|---------|------|--------|
| v2.3.0 | Apr 4 2026 | Universal preflight checklist in place_trade() — Man City lesson |
| v2.2.9 | Apr 4 2026 | SPORTS_KEYWORDS 111→380 (all fee market categories) |
| v2.2.8 | Apr 4 2026 | Match-day sports longshot guard (3 layers) |
| v2.2.7 | Apr 4 2026 | PRIORITY_WATCHLIST uses CLOB token IDs directly |
| v2.2.5 | Apr 3 2026 | PRIORITY_WATCHLIST — ceasefire markets bypass yes_p<0.06 gate |
| v2.2.4 | Apr 3 2026 | Capital limits raised to match $4,700 equity base |
| v2.2.3 | Apr 3 2026 | Pre-simulation constitution validation (6 hard rules) |
| v2.2.2 | Apr 3 2026 | OPERATING_CONSTITUTION.md injected into optimizer Claude prompts |
| v2.2.1 | Apr 3 2026 | post_trade_review failure traces injected into optimizer |
| v2.2.0 | Apr 1 2026 | Hungary guardrail + post_trade_review logger |
| v2.1.4 | Apr 1 2026 | LP max_inventory cap per market |
| v2.1.3 | Mar 31 2026 | LP quoter position conflict check |
| v2.1.2 | Mar 30 2026 | LP quoter GTD orders, 1.5-tick pullback, activity guard |
| v2.1.1 | Mar 29 2026 | Safe GS026 fix — eth_keys.sign_msg_hash |
| v2.1.0 | Mar 28 2026 | Token optimizations, GitHub cleanup |
| v2.0.0 | Mar 27 2026 | LP quoter, merge_apr30, whale scanner |

## Upcoming: CTF + CLOB v2 Migration (2-3 weeks)

Polymarket announced a full exchange stack upgrade. [Announcement](https://x.com/PolymarketDevs/status/2041178623948808693)

**Impact on this agent:**

| Component | Change needed | When |
|-----------|---------------|------|
| `py_clob_client` | `pip install --upgrade py-clob-client` when v2 SDK drops | Before migration day |
| `CTF_EXCHANGE_ADDRESS` in `.env` | Update to new V2 contract address | Migration day |
| `COLLATERAL_TOKEN` | New "Polymarket USD" token — wrap USDC.e → pmUSD | Migration day |
| Open LP orders | All cleared during maintenance window — auto-re-quoted next cron | Automatic |

**Steps on migration day:**
1. Polymarket publishes new contract addresses
2. Update `.env`: `CTF_EXCHANGE_ADDRESS=<new_v2_address>`
3. `pip install --upgrade py-clob-client` on server
4. `systemctl restart polymarket`
5. LP quoter re-quotes automatically on next 4h cycle

**What changed in code (v2 prep):**
- `CTF_EXCHANGE` in `autotrader.py` now reads from `CTF_EXCHANGE_ADDRESS` env var (defaults to current V1 address)
- On migration day: set env var, restart — no code deployment needed
