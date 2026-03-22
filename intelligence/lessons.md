# Learned Lessons
*Empirical findings from 200 trade records — March 2026*

---

## Sports Markets — Conditional (strict policy applies)

**Historical PnL:** -$799 total (NBA/NHL -$463, soccer -$204, college -$132)
**Re-enabled under:** soul.md Sports Trading Policy [S1]–[S17]

### What caused the losses (never repeat)
- **Overconfidence at high NO prices:** Knicks NO @ 0.88, Lightning NO @ 0.88 — market already knew the favorite. LLM "found edge" by agreeing with consensus. [Blocked by S6: NO ≤ 0.75]
- **Lottery ticket YES buys:** NC State @ 0.035, 76ers @ 0.08, USA Baseball @ 0.25 — "could cause upset" is not edge. [Blocked by S5: YES ≥ 0.25]
- **In-play churn:** Penguins/Hurricanes -$152 — traded on price swings the LLM couldn't interpret. [Blocked by S3: no live markets]
- **Both sides same game:** O/U 233.5 + 234.5 on same Spurs/Kings game — guaranteed to lose one side. [Blocked by S13: one trade per game]
- **No typed signal:** Most sports losses had zero verifiable catalyst. General "team quality" was the entire thesis. [Blocked by S7: typed signals required]

### When sports CAN have edge
- Pre-game, 24h+ before tip-off, confirmed starting lineup change not yet priced
- Significant injury news (key starter out) that moved the line ≥5pp in your direction
- Volume ≥ $5k confirms real market participants have priced it

---

## Macro/Geopolitical Markets — Strong Edge

**Historical PnL:** +$703 (Iran +$202, Crude Oil +$501)
- Iran conflict markets: news cycle has genuine 1–3 hour lag vs market repricing
- Commodity NO markets (crude oil, gold): thesis-driven, weeks of runway, strong edge
- Fed/political resolution markets: moderate edge when catalyst is confirmed

**Keep doing:** large NO positions on commodity overshoot targets (oil $110, gold $4200) with weeks of runway

---

## Sizing & Re-entry

- Never re-enter the same market within 24 hours — fragments position, wastes spread (Fed market lesson: 21 micro-trades, same thesis, -$17)
- Buying opposite side of existing position destroys capital
- Stop losses should fire once — check for open sell orders before placing new ones

---

## Edge Quality

- "I agree with the market" is NOT edge — market already priced the consensus view
- "I can imagine this happening" is NOT edge — base rate already reflected in price
- Real edge = "I have a specific catalyst the market hasn't repriced yet"
- Consensus beats single opinion — require 2/3 source agreement before trading

---

## Price Level Guards (global, all markets)

- NO price > 0.82: skip — collecting 18¢ premium on tail risk not worth blow-up
- YES price < 0.08: skip unless confirmed breaking catalyst directly enabling outcome
- Sports-specific: NO price > 0.75 → PASS (tighter than global guard)
- Sports-specific: YES price < 0.25 → PASS

---

## General

- Unrealized losses count toward drawdown — mark-to-market matters
- Geopolitical and commodity markets are the profit engine — allocate there
- Thesis cache must persist to disk — in-memory cache causes buy-then-sell churn on restart
- New positions get 4h grace period before thesis-invalidation check can sell them
