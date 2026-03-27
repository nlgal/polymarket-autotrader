# Trading Lessons (lessons.md)
# Auto-maintained by strategy_optimizer.py
# Every significant loss or system error becomes a permanent rule here.
# Claude reads this at the start of every scan to prevent repeating mistakes.

## Lesson 1: Near-term YES on conflict events always lose
**Date:** March 2026
**What happened:** Bot bought YES on ceasefire Mar 31, forces enter Mar 31, conflict ends Mar 31, US invade Iran Mar 31. All expired worthless or near-worthless.
**Loss:** ~$250 combined
**Rule:** NEVER buy YES on conflict/event markets with < 30 days until expiry. The status quo wins ~75% of the time. Something specific must happen for YES to win — that rarely happens in 30 days.
**Guardrail added:** Hard block in autotrader.py and opportunity_scanner.py

## Lesson 2: Sports markets without insider data are coin flips
**Date:** March 2026
**What happened:** Bought Miami Open tennis (Tommy Paul vs Fils) YES for $69. Lost $64.
**Loss:** $64
**Rule:** Never trade sports markets without Unusual Whales whale/insider flow signal. No edge without data.
**Guardrail added:** Sports pre-filter in Tier 3 of opportunity scanner

## Lesson 3: Token launch / FDV markets have ghost order books
**Date:** March 2026
**What happened:** Backpack FDV $200M NO showed $36k liquidity on gamma API. Bids were only at 0.001-0.05¢ — untradeable. Token launched at $2B FDV, position went to zero.
**Loss:** $125
**Rule:** Never enter markets with "fdv", "tge", "token launch", "fully diluted", "market cap on launch" in the question. Ghost order books look liquid but aren't.
**Guardrail added:** FDV blacklist in autotrader and scanner

## Lesson 4: Bot can buy the same position in a loop
**Date:** March 2026
**What happened:** Scanner placed 10 consecutive BUY orders on "US invade Iran Mar 31 YES" in 12 minutes. The market had near-zero bids so it kept matching at 0.1¢ per share.
**Loss:** $75 (recovered most via sell)
**Rule:** "invade", "attack iran", "bomb iran" must be in the conflict event blocklist. Also: the per-market exposure cap prevents re-entering a position already held.
**Guardrail added:** Extended keyword blocklist; per-market cap already existed

## Lesson 5: pyc bytecode cache causes stale code execution
**Date:** March 2026
**What happened:** Updated opportunity_scanner.py on disk, syntax verified OK, but server kept running the old compiled .pyc version. Bug persisted for hours.
**Rule:** Always clear __pycache__ after deploying a new scanner version. The deploy script (place_wellington_seoul.py) now does this automatically.
**Fix:** Deploy script clears opportunity_scanner.cpython-*.pyc files after every update

## Lesson 6: Crude Oil NO — don't hold near the price trigger
**Date:** March 27, 2026
**What happened:** Held Crude Oil HIGH $100 NO when WTI was at $99.24 (4 days before expiry). Position was at $47 value vs $182 cost. Sold for $44.
**Loss:** $138 (locked in at sale)
**Rule:** When a NO position's trigger price is within 2% of current price with < 7 days to expiry, EXIT immediately. The risk/reward is not worth holding.
**Guardrail needed:** Health monitor should alert when NO position is within 2% of trigger

## Lesson 7: Executor single-threaded hang
**Date:** March 2026
**What happened:** Executor HTTP server hung when exit_contradictions.py made a blocking CLOB API call inside the HTTP handler. Port accepted TCP but returned zero bytes. All remote operations blocked for 12+ hours.
**Rule:** Executor must use ThreadingMixIn (one thread per request). Any long-running script should never block the HTTP handler thread.
**Fix:** executor.py updated to use ThreadingHTTPServer with daemon_threads=True

## Lesson 8: Ceasefire YES positions lose even with good news
**Date:** March 2026
**What happened:** Bought Ceasefire Apr 15 YES at 69¢ (127 shares). Iran rejected US 15-point plan with 5 counter-demands. Lost $146 — biggest single trade loss.
**Rule:** Even "good news" (ceasefire plans offered) doesn't mean the deal closes. Iran rejecting deals is the base case. Only buy ceasefire YES if there's strong UW insider signal AND a deal is structurally agreed (not just offered).
**Guardrail:** Ceasefire YES with < 30 days blocked. Longer-duration ceasefire YES requires UW signal.

## Summary Stats (as of March 27, 2026)
- Total capital: $1,600 deposited
- Current equity: ~$1,171
- Total loss: ~$429 (-26.8%)
- Main loss drivers: conflict event YES bets (3 biggest losses totaling ~$350)
- Win rate on closed trades: ~64%
- The system works when trading NOs on status-quo events and long-duration YESes on escalation

## Lesson 9: Scanner bought commodity NO after trigger was already hit
**Date:** March 27, 2026
**What happened:** Scanner bought Crude Oil $100 NO for $75 when WTI was already at $101.18. The Nash Equilibrium scoring gave edge=0.19 because news RSS data didn't reflect live commodity prices. Position was nearly worthless immediately.
**Loss:** ~$71 (sold for $4 from $75 cost)
**Rule:** Before scoring any commodity price market, check the LIVE price. If WTI >= target price, NO is worthless. Do not rely on LLM scoring alone for factual price checks.
**Guardrail added:** check_commodity_reality() in Tier 3 pre-filter — fetches live WTI/Gold from Yahoo Finance before any Claude call on commodity markets.

## Lesson 10: Commodity check used yes_p as proxy for trade direction — wrong
**Date:** March 27, 2026
**What happened:** Scanner bought Crude Oil $100 NO for $75 when WTI was at $99.64 (just $0.36 from trigger). The commodity reality check (Lesson 9's fix) used `yes_p < 0.5` as a proxy for "we're buying NO". But yes_p was 0.57 — so the check NEVER FIRED. The bot still bought NO with price essentially at the trigger.
**Loss:** ~$75 at risk (position still open, likely to lose if WTI settles at $100 before March 31)
**Root cause:** The bug: `if yes_p < 0.5 and wti >= target * 0.99` — this means "only block if market says NO is likely AND price is near trigger". But when market says YES is 57% likely, that's exactly when price is closest to trigger and NO is most dangerous.
**Rule:** For commodity HIGH/LOW target markets, if price is within $5 of the trigger, SKIP regardless of yes_p. The proximity to trigger is what matters, not the market's probability direction.
**Guardrail added:** check_commodity_reality() now uses `abs(gap) <= 5.0` hard block — skips if price is within $5 of trigger in either direction, independent of yes_p.
