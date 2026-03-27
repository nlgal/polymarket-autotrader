# Polymarket Autotrader — Context File
# Loaded by every Claude call in the opportunity scanner and strategy optimizer.
# Last updated: 2026-03-27

## What This Bot Does
Autonomous prediction market trader on Polymarket (Polygon blockchain).
- Wallet: Gnosis Safe `0xc2c1892653C175113c65961C7F4227c18D09b52a`
- Runs on DigitalOcean server (Amsterdam) as systemd service
- Capital: ~$1,200 equity (started with $1,600, down ~25%)
- Goal: grow to $100k through disciplined edge-based trading

## Trading Rules — NON-NEGOTIABLE

### NEVER DO
- BUY YES on conflict/event markets with < 30 days until expiry
  - Covers: ceasefire, forces enter, regime fall, invasion, invade, war ends, kharg, hormuz, military operations, nuclear deal, peace deal, attack iran, bomb iran
  - Reason: "something must happen by deadline" bets lose ~75% of the time. Status quo wins.
- Trade sports markets without Unusual Whales whale flow signal
  - Reason: Miami Open tennis bet lost $64 with zero edge. Pure coin-flip without data.
- Buy near-certain markets (>93¢ YES or <7¢ YES)
  - No edge. Fees eat the premium.
- Buy coin-flip markets (42-58¢ YES range) without a clear signal
  - Reason: Without news catalyst or UW signal, these are random noise.
- Enter FDV/TGE/token launch markets
  - Reason: Ghost order books. Backpack FDV cost $125.

### ALWAYS DO
- Prefer NO on conflict markets — status quo bets win reliably
- Check Unusual Whales signals before scoring any market
  - "insider_trades" or "contrarian_whales" tags = strong signal, lower edge threshold
  - Whale positions >$50k = directional confirmation
- Require real CLOB depth (bids AND asks between 10¢-90¢, spread < 30¢)
- Keep position sizes small when portfolio < $2,000
  - MAX_TRADE_SIZE = $75 (at current equity level)
  - Never more than 30% of free cash in one trade

## What Has Worked (Profitable Patterns)
1. **Iran conflict status-quo NOs** — holding NO on short-duration events that require action
   - Example: Ceasefire Mar 31 NO: +$213 (entered at 58¢, exited at 87¢)
   - Example: Iranian regime fall Jun 30 NO: +$114
2. **BTC 5-minute momentum** — the BTC Up/Down 5-min markets
   - Bitcoin momentum trades: +$24, +$24, +$24, +$17, +$14... consistent small wins
   - Win rate ~70% on momentum signal
3. **Long-duration YES on forces/escalation** — US forces enter Iran by April 30 YES
   - Entered at 54.5¢, now 62.5¢ (+$44). 35 days remaining, Marines deploying.
4. **Ceasefire April 30 NO** — entered at 49¢, now 59¢ (+$39)
   - Iran rejected US 15-point plan, issued 5 counter-demands. Deal very unlikely.

## What Has Failed (Loss Patterns)
1. **Short-duration YES on conflict events** — the biggest loss category
   - Ceasefire Apr 15 YES: -$146 (bought at 69¢, expired worthless after Iran rejected)
   - US forces Dec 31 NO: -$96 (wrong direction + 9-month market moved against us)
   - US forces Mar 31 YES: -$37 (no ground invasion in 5 days)
   - US invade Iran Mar 31 YES: bot bought 10x in a loop (fixed: keyword guardrail)
2. **Sports without data** — Miami Open tennis: -$64
3. **Kharg Island Mar 31 YES** — -$40 (event-driven, didn't happen)

## Current Portfolio (as of 2026-03-27)
| Position | Value | P&L | Expires |
|---|---|---|---|
| US forces enter Iran Apr 30 YES | $343 | +$44 | Apr 30 |
| US x Iran ceasefire Apr 30 NO | $230 | +$39 | Apr 30 |
| Crude Oil HIGH $100 NO | $188 | +$6 | Mar 31 |
| Aliens exist before 2027 YES | $145 | -$5 | Dec 31 |
| Ceasefire Apr 15 NO | $138 | +$11 | Apr 15 |
| Regime fall Jun 30 NO | $75 | +$1 | Jun 30 |
| US forces Dec 31 NO | $69 | -$6 | Dec 31 |

## Current Market Context (Iran War)
- US deployed warships + Marines to Iran region (WSJ, March 25)
- Iran rejected US 15-point ceasefire plan, issued 5 counter-demands
- Trump declared war "won" but airstrikes continue
- No signed ceasefire. War is ongoing as of March 27, 2026.
- Crude oil at ~$90 (well below $100 threshold — our NO is winning)

## Scoring Guidelines for Claude
When asked to score a Polymarket market:

**Strong BUY_NO signals:**
- Near-term deadline for a conflict event (< 30 days)
- Status quo has been stable for weeks
- Iran has publicly rejected negotiations
- Market is pricing YES too high for near-term resolution

**Strong BUY_YES signals:**
- Long duration (> 30 days) on escalation events (military buildup, etc.)
- UW shows insider_trades + contrarian_whales tags
- Clear news catalyst in last 24h with actual action (not just talks)
- Price is below 35¢ for events that seem likely given escalation

**Strong PASS signals:**
- Sports market without UW whale flow
- Coin-flip range (42-58¢) without clear catalyst
- Market about to expire in < 7 days
- FDV, TGE, token launch, celebrity tweet count markets

## System Architecture
- autotrader.py: main 15-min trading loop (scores markets, places BUY/SELL orders)
- opportunity_scanner.py: 2-hour deep scan (25 markets, UW signals, pre-filter + Claude scoring)
- strategy_optimizer.py: daily Karpathy Loop (scores config against trade history, proposes tweaks)
- scanner_config.json: tunable parameters (edge threshold, trade sizes, pre-filter rules)
- executor.py: HTTP server on port 8888 (deploys scripts, runs commands remotely)
- health monitor: hourly Telegram alerts on errors, portfolio issues, breaking news

## Edge Thresholds (Current Config)
- MIN_SCAN_EDGE = 0.15 (only very high conviction — 15% mispricing required)
- MAX_TRADE_SIZE = $75 per trade
- UW_EDGE_DISCOUNT = 0.20 (lower threshold to 12% when insider/whale signal present)
- COIN_FLIP_BAND = [0.42, 0.58] (skip these without signal)
- CONFLICT_EVENT_MIN_DAYS = 30 (block YES on conflict markets with < 30 days)

## The One Rule That Matters Most
"The status quo is almost always the correct prediction for near-term conflict markets.
Nothing needs to happen for NO to win. Something specific must happen for YES to win.
Near-term event markets are stacked against YES buyers."
