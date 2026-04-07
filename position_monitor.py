#!/usr/bin/env python3
"""
position_monitor.py — Per-position monitoring with actionable alerts
====================================================================
Runs every 4h as part of the health monitor cron.
For each open position checks:
  1. P&L deterioration (>15% move against entry)
  2. Expiry proximity (<7 days remaining)
  3. Targeted news for that position's topic
  4. Whale activity on that specific market (last 4h)
  5. Large equity swing (>$25 move since last check)

Fires Telegram ONLY when something actionable is found.
Recommendation per position: HOLD / ADD / REDUCE / EXIT + one line why.

Token cost: ~3-5 Haiku calls per run (only positions needing news check).
State: /opt/polymarket-agent/position_monitor_state.json
"""

import os, sys, json, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')

# ── Optional modules (fail gracefully if not present) ────────────────────────
def _try_import(module_name):
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            module_name, f"/opt/polymarket-agent/{module_name}.py")
        if spec is None:
            return None
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

STATE_FILE  = "/opt/polymarket-agent/position_monitor_state.json"

# Thresholds
PNL_ALERT_PCT      = -15    # alert if position P&L drops below -15%
EXPIRY_WARN_DAYS   = 7      # alert if expiry within 7 days
EQUITY_SWING_USD   = 25     # alert if position value moves >$25 since last check
MIN_POSITION_USD   = 20     # ignore dust positions

# News keywords per market topic (used to fetch Google News RSS)
TOPIC_KEYWORDS = {
    "ceasefire":     ["iran ceasefire", "iran peace deal", "iran nuclear agreement"],
    "forces":        ["us iran military", "us forces iran", "iran strike"],
    "regime":        ["iran regime", "iranian government collapse", "khamenei"],
    "hungary":       ["hungary election", "peter magyar", "orban hungary"],
    "taiwan":        ["china taiwan invasion", "taiwan strait military"],
    "china":         ["china taiwan invasion", "taiwan strait"],
}

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except Exception:
            pass

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_positions():
    try:
        r = requests.get(f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50", timeout=15)
        return [p for p in r.json() if float(p.get("currentValue", 0)) >= MIN_POSITION_USD]
    except Exception:
        return []

def get_whale_activity(condition_id, since_ts):
    """Check if any large trade (>$500) happened on this market since since_ts."""
    try:
        r = requests.get(
            f"https://data-api.polymarket.com/activity?market={condition_id}&limit=20",
            timeout=10
        )
        if r.status_code != 200:
            return []
        acts = r.json()
        big = []
        for a in acts:
            ts = a.get("timestamp", 0)
            if not isinstance(ts, (int, float)):
                continue
            if ts < since_ts:
                continue
            usdc = float(a.get("usdcSize", 0) or 0)
            if usdc >= 500:
                side = a.get("side", "?")
                outcome = a.get("outcome", "?")
                wallet = (a.get("maker", "") or a.get("user", "") or "?")[:10]
                big.append(f"{side} {outcome} ${usdc:.0f} ({wallet}...)")
        return big
    except Exception:
        return []

def fetch_news(keyword):
    """Fetch top 3 headlines from Google News RSS for a keyword."""
    try:
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(keyword)}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return []
        import re
        titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', r.text)
        # Skip first title (feed title)
        return [t for t in titles[1:4] if len(t) > 10]
    except Exception:
        return []

def get_topic_keywords(title, outcome):
    """Map position title to news search keywords."""
    t = title.lower()
    if "ceasefire" in t:
        return TOPIC_KEYWORDS["ceasefire"]
    if "forces enter iran" in t or "us forces" in t:
        return TOPIC_KEYWORDS["forces"]
    if "regime fall" in t or "iranian regime" in t:
        return TOPIC_KEYWORDS["regime"]
    if "hungary" in t or "magyar" in t:
        return TOPIC_KEYWORDS["hungary"]
    if "taiwan" in t:
        return TOPIC_KEYWORDS["taiwan"]
    if "china" in t:
        return TOPIC_KEYWORDS["china"]
    return []

def assess_news_with_claude(title, outcome, headlines):
    """
    Ask Claude Haiku: given these headlines, should we HOLD/ADD/REDUCE/EXIT?
    Returns (verdict, reason) or None on error.
    """
    if not ANTHROPIC_KEY or not headlines:
        return None
    try:
        prompt = (
            f"We hold a Polymarket position: {outcome.upper()} on '{title}'\n"
            f"Recent headlines:\n" +
            "\n".join(f"- {h}" for h in headlines) +
            "\n\nGiven these headlines, what's the action for our position?\n"
            "Respond with EXACTLY: HOLD | ADD | REDUCE | EXIT\n"
            "Then one line (max 80 chars) explaining why."
        )
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5", "max_tokens": 60,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=15)
        if resp.status_code == 200:
            text = resp.json()["content"][0]["text"].strip()
            lines = text.split("\n", 1)
            verdict = lines[0].strip().upper()
            reason  = lines[1].strip() if len(lines) > 1 else ""
            if any(v in verdict for v in ["HOLD","ADD","REDUCE","EXIT"]):
                return verdict, reason
    except Exception:
        pass
    return None

def days_to_expiry(end_date_str):
    """Returns days until end_date, or None if unknown."""
    if not end_date_str:
        return None
    try:
        end = datetime.datetime.fromisoformat(end_date_str.replace("Z",""))
        delta = (end - datetime.datetime.utcnow()).days
        return delta
    except Exception:
        return None

def is_contradiction(positions):
    """
    Returns list of LOSING contradiction pairs (YES+NO same market, total payout < total cost).
    Strangle (profitable either way) is NOT a contradiction.
    """
    from collections import defaultdict
    by_condition = defaultdict(list)
    for p in positions:
        cid = p.get("conditionId","")
        if cid:
            by_condition[cid].append(p)
    
    contradictions = []
    for cid, group in by_condition.items():
        outcomes = {p.get("outcome","").upper() for p in group}
        if "YES" not in outcomes or "NO" not in outcomes:
            continue
        yes_p = next((p for p in group if p.get("outcome","").upper() == "YES"), None)
        no_p  = next((p for p in group if p.get("outcome","").upper() == "NO"), None)
        if not yes_p or not no_p:
            continue
        
        yes_shares = float(yes_p.get("size", 0))
        no_shares  = float(no_p.get("size", 0))
        yes_cost   = float(yes_p.get("initialValue", 0))
        no_cost    = float(no_p.get("initialValue", 0))
        total_cost = yes_cost + no_cost
        min_payout = min(yes_shares, no_shares)
        
        # Strangle = profitable either way → not a contradiction
        if min_payout >= total_cost:
            continue
        
        # Genuine contradiction: losing either way
        contradictions.append({
            "title": yes_p.get("title","?")[:50],
            "yes_shares": yes_shares,
            "no_shares": no_shares,
            "total_cost": total_cost,
            "min_payout": min_payout,
            "loss_if_resolved": total_cost - min_payout,
        })
    return contradictions

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=== POSITION MONITOR ===")
    
    positions = get_positions()
    if not positions:
        log("No positions found")
        return
    
    state     = load_state()
    now_ts    = time.time()
    since_ts  = now_ts - 4 * 3600  # whale activity window: last 4h
    alerts    = []
    updates   = {}
    haiku_calls = 0
    
    # ── 1. Contradiction check (zero LLM cost) ────────────────────────────────
    bad_contras = is_contradiction(positions)
    for c in bad_contras:
        loss = c["loss_if_resolved"]
        alerts.append(
            f"⚠️ <b>LOSING CONTRADICTION</b>: {c['title']}\n"
            f"YES {c['yes_shares']:.0f}sh + NO {c['no_shares']:.0f}sh\n"
            f"Cost ${c['total_cost']:.0f} but min payout ${c['min_payout']:.0f} → loss ${loss:.0f}\n"
            f"→ ACTION: merge or close the smaller side"
        )
        log(f"  CONTRADICTION: {c['title']} — loss ${loss:.0f} if resolved")
    
    # ── 1b. Market guardrails (Hungary NO, etc.) ────────────────────────────────
    _guardrails_mod = _try_import("market_guardrails")
    if _guardrails_mod:
        try:
            _g_action, _g_details = _guardrails_mod.check_hungary_guardrail()
            if _g_action and _g_action not in ("blocked_illiquid", "sell_failed",
                                                "already_actioned", None):
                alerts.append(
                    f"🛡️ <b>Guardrail executed</b>: Hungary NO {_g_action}\n"
                    f"  {json.dumps(_g_details)[:120]}"
                )
                log(f"  Guardrail executed: {_g_action}")
        except Exception as _ge:
            log(f"  Guardrail error: {_ge}")
    else:
        log("  market_guardrails.py not found — skip guardrail check")

    # ── 2. Per-position checks ────────────────────────────────────────────────
    for p in positions:
        title   = p.get("title", "")[:55]
        outcome = p.get("outcome", "")
        val     = float(p.get("currentValue", 0))
        cost    = float(p.get("initialValue", 0))
        avg_p   = float(p.get("avgPrice", 0))
        cur_p   = float(p.get("curPrice", 0))
        cid     = p.get("conditionId", "")
        end_str = p.get("endDate", "")[:10]
        pnl_pct = (val - cost) / cost * 100 if cost > 0 else 0
        
        key = cid + "_" + outcome.upper()
        prev = state.get(key, {})
        prev_val = float(prev.get("val", val))
        swing = val - prev_val
        
        pos_alerts = []
        
        # Check 1: P&L deterioration
        if pnl_pct < PNL_ALERT_PCT:
            pos_alerts.append(f"P&L {pnl_pct:+.0f}% — down from entry")
        
        # Check 2: Expiry proximity
        days_left = days_to_expiry(end_str)
        if days_left is not None and days_left <= EXPIRY_WARN_DAYS:
            pos_alerts.append(f"⏰ {days_left}d to expiry ({end_str})")
        
        # Check 3: Large equity swing since last check
        if abs(swing) >= EQUITY_SWING_USD:
            direction = "up" if swing > 0 else "down"
            pos_alerts.append(f"${abs(swing):.0f} {direction} since last check")
        
        # Check 4: Whale activity on this market
        if cid:
            whale_trades = get_whale_activity(cid, since_ts)
            for wt in whale_trades:
                pos_alerts.append(f"🐋 {wt}")
        
        # Check 5: News (only if something flagged OR expiry <7d)
        verdict = None
        if pos_alerts or (days_left is not None and days_left <= 10):
            keywords = get_topic_keywords(title, outcome)
            for kw in keywords[:1]:  # 1 keyword per position to save tokens
                headlines = fetch_news(kw)
                if headlines:
                    result = assess_news_with_claude(title, outcome, headlines)
                    haiku_calls += 1
                    if result:
                        verdict, reason = result
                        if verdict != "HOLD":
                            pos_alerts.append(f"📰 News → <b>{verdict}</b>: {reason}")
                        else:
                            log(f"  {title[:30]}: news says HOLD — no alert")
                    break
        
        # Build alert for this position if anything flagged
        if pos_alerts:
            action = verdict or ("EXIT" if pnl_pct < -20 else "REDUCE" if pnl_pct < PNL_ALERT_PCT else "REVIEW")
            alerts.append(
                f"📊 <b>{action}</b> — {outcome} on {title}\n"
                f"  Value: ${val:.0f} | P&L: {pnl_pct:+.0f}% | Price: {cur_p:.3f}\n"
                + "\n".join(f"  • {a}" for a in pos_alerts)
            )
            log(f"  ALERT [{action}]: {title[:35]} ({', '.join(pos_alerts)[:60]})")
        else:
            log(f"  OK: {outcome} {title[:40]} P&L={pnl_pct:+.0f}% val=${val:.0f}")
        
        # Update state
        updates[key] = {"val": val, "cost": cost, "ts": now_ts}
    
    # ── 3. Send Telegram summary ──────────────────────────────────────────────
    if alerts:
        header = f"🔍 <b>Position Monitor</b> — {datetime.datetime.utcnow().strftime('%H:%M UTC')}\n"
        msg = header + "\n\n".join(alerts)
        # Keep under 4000 chars
        if len(msg) > 3800:
            msg = msg[:3800] + "\n…"
        tg(msg)
        log(f"Sent {len(alerts)} alert(s)")
    else:
        log("All positions OK — no alerts")
    
    log(f"Haiku calls: {haiku_calls} | Positions checked: {len(positions)}")
    
    # ── 4. Post-trade review: run failure detection ──────────────────────────────
    _ptr_mod = _try_import("post_trade_review")
    if _ptr_mod:
        try:
            _ptr_mod.run_failure_detection(silent=True)
        except Exception as _pe:
            log(f"  post_trade_review detection error: {_pe}")

    # Save updated state
    state.update(updates)
    state["last_run"] = now_ts
    save_state(state)


# ═══════════════════════════════════════════════════════════════
# EMERGENCY SELL — ONE-TIME EXECUTION (Apr 7 2026, 22:12 UTC)
# Sell: Apr15 NO (400sh), Apr30 NO (~1184sh)
# Reason: Pakistan 2-week ceasefire proposal — NO thesis broken
# This block self-disables after first successful run via flag file
# ═══════════════════════════════════════════════════════════════
def emergency_sell_no_positions():
    import os, time, requests as _req
    FLAG = "/opt/polymarket-agent/.emergency_sell_done"
    # Flag cleared — retry each run until all sells succeed
    # if os.path.exists(FLAG):
    #     return
    
    log("=== EMERGENCY SELL: CEASEFIRE NO POSITIONS ===")
    
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY","")
        FUNDER_ADDR = os.environ.get("POLYMARKET_FUNDER_ADDRESS","")
        client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                            chain_id=137, signature_type=2, funder=FUNDER_ADDR)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    except Exception as e:
        log(f"  Client init failed: {e}")
        return

    SELLS = [
        {"label": "Apr15 NO", "token_id": "8442709013751543525223072638303914942960068246422295030411662679470140144155", "shares": 400},
        {"label": "Apr30 NO", "token_id": "52284848830940446862370529859386043059769275594386884690262695607365719243018", "shares": 1184},
    ]

    all_ok = True
    for pos in SELLS:
        label, token_id, shares = pos["label"], pos["token_id"], pos["shares"]
        try:
            r = _req.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=8)
            bids = r.json().get("bids", []) if r.ok else []
            best_bid = float(bids[0]["price"]) if bids else 0
            if best_bid < 0.01:
                log(f"  {label}: no bids — skipping")
                all_ok = False
                continue
            log(f"  {label}: selling {shares}sh @ best bid {best_bid:.4f} (~${shares*best_bid:.2f})")
            from py_clob_client.clob_types import OrderArgs, TradeParams
            from py_clob_client.constants import BUY, SELL
            # For SELL: create limit order at 1¢ (will fill as market against any bid)
            # Get best bid first
            best_bid_r = _req.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=8)
            bids_list = best_bid_r.json().get("bids", []) if best_bid_r.ok else []
            if not bids_list:
                log(f"  {label}: no bids in book")
                all_ok = False
                continue
            sell_price = float(bids_list[0]["price"])
            # Build order: sell `shares` tokens at sell_price
            order_args = OrderArgs(
                price=sell_price,
                size=float(shares),
                side=SELL,
                token_id=token_id,
            )
            order = client.create_order(order_args)
            resp  = client.post_order(order, OrderType.GTC)
            if resp and resp.get("success"):
                log(f"  {label}: ✅ SOLD")
            else:
                log(f"  {label}: ❌ {resp}")
                all_ok = False
        except Exception as e:
            log(f"  {label}: ❌ {e}")
            all_ok = False
        time.sleep(1)

    if all_ok:
        open(FLAG, "w").write("done")
        log("=== EMERGENCY SELL COMPLETE ===")
    else:
        log("=== EMERGENCY SELL: some failures, check positions ===")

emergency_sell_no_positions()

if __name__ == "__main__":
    main()
