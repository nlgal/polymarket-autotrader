"""
opportunity_scanner.py
======================
Autonomous opportunity scanner — runs every 2 hours via cron.
Scans Polymarket for mispriced markets using news + UW whale/insider signals.
Places trades directly when edge > threshold, sends Telegram summary.

Strategy:
- Pulls top 50 markets by liquidity/volume
- Augments with UW whale/unusual/insider signals from Unusual Whales API
- For each candidate: fetches news, UW signal, scores edge with Claude
- Places BUY_NO or BUY_YES when edge >= threshold (lower if UW insider flag)
- Reports to Telegram regardless (good or no-trade)

Runs on server via executor. Uses same .env as autotrader.
"""
import os, sys, re, json, math, time, datetime, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

# ── Config ────────────────────────────────────────────────────────────────────
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN","").strip()
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID","").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY","").strip()
UW_API_KEY    = os.environ.get("UW_API_KEY","").strip()
UW_BASE       = "https://api.unusualwhales.com"

MIN_SCAN_EDGE    = 0.15   # Raised from 0.12 — only very high conviction trades's 0.07 — only obvious mispricings
MIN_LIQUIDITY    = 50000  # $50k minimum liquidity
MAX_TRADE_SIZE   = 75     # Max USDC per trade — capped to reduce loss per bad bet
MIN_TRADE_SIZE   = 35
UW_EDGE_DISCOUNT = 0.20   # Lower edge threshold by 20% when UW insider/whale signal present
ALREADY_IN_FILE  = "/opt/polymarket-agent/intelligence/existing_positions.json"
SCAN_LOG         = "/opt/polymarket-agent/opportunity_scan.log"

def tg(msg, parse_mode="HTML"):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": parse_mode}, timeout=10)
        except: pass

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(SCAN_LOG, "a") as f:
            f.write(line + "\n")
    except: pass

def get_usdc_balance():
    """Get available USDC cash."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                        chain_id=137, signature_type=2, funder=FUNDER)
    c = client.create_or_derive_api_creds()
    client.set_api_creds(c)
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL, signature_type=2))
    return float(bal.get("balance", 0)) / 1e6, client

# ── Unusual Whales Prediction Market Signal ─────────────────────────────────────

def get_uw_signals():
    """
    Fetch unusual + whale signals from Unusual Whales prediction market endpoints.
    Returns dict keyed by asset_id (CLOB token ID).
    """
    if not UW_API_KEY:
        return {}
    headers = {"Authorization": f"Bearer {UW_API_KEY}", "Accept": "application/json"}
    signals = {}

    # Unusual prediction markets (insider_trades, contrarian_whales tags)
    try:
        r = requests.get(f"{UW_BASE}/api/predictions/unusual", headers=headers, timeout=10)
        if r.status_code == 200:
            for item in r.json().get("data", {}).get("data", []):
                asset_id = item.get("asset_id", "")
                if not asset_id:
                    continue
                tags = [t.get("tag", "") for t in item.get("tags", [])]
                signals[asset_id] = {
                    "source": "unusual",
                    "tags": tags,
                    "smart_volume": float(item.get("smart_volume", 0)),
                    "outcome": item.get("outcome", "Yes"),
                    "current_price": float(item.get("current", 0) or 0),
                    "market": item.get("market", ""),
                }
    except Exception as e:
        log(f"[UW] Unusual fetch error: {e}")

    # Whale positions ($50k+ smart money)
    try:
        r = requests.get(f"{UW_BASE}/api/predictions/whales", headers=headers, timeout=10)
        if r.status_code == 200:
            for item in r.json().get("data", {}).get("data", []):
                asset_id = item.get("asset_id", "")
                if not asset_id:
                    continue
                invested = float(item.get("invested_usd", 0))
                if invested < 50000:
                    continue
                existing = signals.get(asset_id, {})
                signals[asset_id] = {
                    **existing,
                    "source": existing.get("source", "whale"),
                    "tags": list(set(existing.get("tags", []) + ["whale_position"])),
                    "whale_invested": invested,
                    "whale_amount": float(item.get("amount", 0)),
                    "outcome": item.get("outcome", existing.get("outcome", "Yes")),
                    "avg_price": float(item.get("avg_price", 0) or 0),
                    "current_price": float(item.get("current_price", existing.get("current_price", 0)) or 0),
                    "market": item.get("market", existing.get("market", "")),
                }
    except Exception as e:
        log(f"[UW] Whale fetch error: {e}")

    log(f"[UW] Loaded {len(signals)} signal(s) from Unusual Whales")
    return signals


def get_uw_signal_for_market(market, uw_signals):
    """Match market tokens against UW signals. Returns (signal or None, action_hint or None)."""
    tokens = market.get("clob_token_ids", [])
    for token in tokens:
        if token in uw_signals:
            sig = uw_signals[token]
            outcome = sig.get("outcome", "Yes").lower()
            is_yes_token = (token == tokens[0]) if len(tokens) > 1 else True
            if is_yes_token:
                action_hint = "BUY_YES" if "yes" in outcome else "BUY_NO"
            else:
                action_hint = "BUY_NO" if "yes" in outcome else "BUY_YES"
            return sig, action_hint
    return None, None


def uw_signal_summary(sig):
    """Format UW signal for Claude prompt injection."""
    if not sig:
        return ""
    parts = []
    whale_inv = sig.get("whale_invested", 0)
    smart_vol = sig.get("smart_volume", 0)
    tags = sig.get("tags", [])
    if whale_inv > 0:
        parts.append(f"Whale position: ${whale_inv:,.0f} invested at avg {sig.get('avg_price', 0):.3f}")
    if smart_vol > 0:
        parts.append(f"Smart money volume: ${smart_vol:,.0f}")
    if "insider_trades" in tags:
        parts.append("⚠️ INSIDER TRADES flagged by Unusual Whales")
    if "contrarian_whales" in tags:
        parts.append("⚠️ CONTRARIAN WHALES flagged by Unusual Whales")
    if "momentum" in tags:
        parts.append("Momentum signal active")
    return "\n".join(parts) if parts else ""


def get_existing_positions():
    """Get markets we already have positions in."""
    try:
        r = requests.get(f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=100", timeout=10)
        return {p.get("conditionId","") for p in r.json()}
    except:
        return set()

def get_candidate_markets():
    """Pull top liquid markets, filter out obvious non-opportunities."""
    r = requests.get(
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100"
        "&order=volume24hr&ascending=false", timeout=15)
    markets = r.json()
    
    candidates = []
    for m in markets:
        liq = float(m.get("liquidity") or 0)
        if liq < MIN_LIQUIDITY:
            continue
        
        try:
            prices = json.loads(m.get("outcomePrices","[]"))
            yes_p = float(prices[0])
        except:
            continue
        
        # Skip near-certain or coin-flip with no news signal
        if yes_p > 0.94 or yes_p < 0.06:
            continue
        
        q = m.get("question","")
        q_lower = q.lower()
        # Skip in-play sports (spread/totals), tweet counts
        if any(x in q_lower for x in ["spread:", "o/u", "tweets", "post from", "how many times"]):
            continue
        # Skip token launches, FDV markets, TGE markets — these resolve before
        # any real order book forms and prices are stale/fake.
        # Lesson: Backpack FDV $200M NO cost $122 because the token launched at
        # $2B FDV but the market showed 46¢ YES with ghost order book depth.
        if any(x in q_lower for x in ["fdv", "tge", "token launch", "token generation",
                                       "fully diluted", "market cap on launch",
                                       "price on launch", "day after launch"]):
            continue
        # Skip same-day game results (already resolving)
        end = m.get("endDate","")
        if end:
            try:
                end_dt = datetime.datetime.fromisoformat(end.replace("Z",""))
                if (end_dt - datetime.datetime.utcnow()).total_seconds() < 3600:
                    continue  # resolves within 1h — skip
            except: pass
        
        candidates.append({
            "question": q,
            "yes_p": yes_p,
            "no_p": 1 - yes_p,
            "liquidity": liq,
            "volume24h": float(m.get("volume24hr") or 0),
            "conditionId": m.get("conditionId",""),
            "clob_token_ids": json.loads(m.get("clobTokenIds","[]") or "[]"),
            "endDate": end,
            "description": m.get("description","")[:400],
        })
    
    return sorted(candidates, key=lambda x: x["volume24h"], reverse=True)[:25]

def load_claude_md():
    """Load CLAUDE.md context file if it exists."""
    claude_md_path = os.path.join("/opt/polymarket-agent", "CLAUDE.md")
    if os.path.exists(claude_md_path):
        try:
            with open(claude_md_path) as f:
                return f.read()[:3000]  # cap at 3000 chars to control token usage
        except: pass
    return ""


# Cache CLAUDE.md at module load time (read once per scan run)
_CLAUDE_MD_CONTEXT = load_claude_md()


def score_with_claude(question, yes_p, description, news_snippets, uw_summary=""):
    """Use Claude to score the edge given fresh news and persistent trading context."""
    if not ANTHROPIC_KEY:
        return "PASS", 0.0, "No API key"
    
    uw_section = f"\nUNUSUAL WHALES SIGNAL:\n{uw_summary}" if uw_summary else ""
    context_section = f"\n\nTRADING CONTEXT (from CLAUDE.md):\n{_CLAUDE_MD_CONTEXT[:1500]}" if _CLAUDE_MD_CONTEXT else ""
    prompt = f"""You are a prediction market analyst. Score the following market and determine if there's a trading edge.

MARKET: {question}
CURRENT YES PRICE: {yes_p:.2f} (implies {yes_p*100:.0f}% probability)
DESCRIPTION: {description}

RECENT NEWS CONTEXT:
{news_snippets}{uw_section}{context_section}

Respond with ONLY a JSON object like this:
{{
  "true_probability": 0.XX,
  "action": "BUY_YES" | "BUY_NO" | "PASS",
  "edge": 0.XX,
  "reasoning": "one sentence"
}}

Rules:
- true_probability = your honest estimate of YES resolving
- edge = abs(true_probability - yes_p) — only flag if > 0.12
- action = BUY_YES if true_prob > yes_p + 0.12, BUY_NO if true_prob < yes_p - 0.12, else PASS
- Be conservative. Only flag genuine mispricings backed by news.
- If Unusual Whales flags INSIDER TRADES or CONTRARIAN WHALES, treat as strong signal.
- For sports/games with no clear favorite signal, PASS."""
    
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5",
                  "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20)
        resp_data = resp.json()
        if resp.status_code != 200:
            err = resp_data.get("error",{}).get("message","API error")[:60]
            return "PASS", 0, f"API {resp.status_code}: {err}"
        content = resp_data.get("content", [{}])[0].get("text","")
        # Extract JSON — handle both raw JSON and ```json blocks
        match = re.search(r'\{[^{}]*"action"[^{}]*\}', content, re.DOTALL)
        if not match:
            match = re.search(r'\{.*?\}', content, re.DOTALL)
        if match:
            result = json.loads(match.group())
            return result.get("action","PASS"), float(result.get("edge",0)), result.get("reasoning","")
        return "PASS", 0, f"no json: {content[:80]}"
    except Exception as e:
        return "PASS", 0, f"err: {str(e)[:60]}"

def fetch_news_snippets(question):
    """Fetch relevant RSS headlines for the market question."""
    try:
        # Use a broad query from the question
        keywords = " ".join(question.split()[:6])
        r = requests.get(
            f"https://news.google.com/rss/search?q={keywords.replace(' ', '+')}&hl=en-US&gl=US&ceid=US:en",
            timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        
        # Parse RSS simple
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text)
        items = root.findall(".//item")
        snippets = []
        for item in items[:5]:
            title = item.findtext("title","")
            pub = item.findtext("pubDate","")[:16]
            snippets.append(f"- [{pub}] {title}")
        return "\n".join(snippets) if snippets else "No recent news found"
    except Exception as e:
        return f"News fetch error: {e}"

def get_clob_price(token_id):
    """Get actual CLOB mid price."""
    try:
        r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=5)
        return float(r.json().get("mid", 0))
    except:
        return 0

def has_real_liquidity(token_id, side="YES", min_depth=2000):
    """
    Check if the order book has REAL depth at tradeable prices.
    Ghost order books (bids at 0.001, asks at 0.999) pass the gamma API
    liquidity check but are untradeable.
    
    Requirements:
    1. At least 3 orders in the 10¢–90¢ price range (not just extreme walls)
    2. Total depth >= min_depth USDC in that range
    3. Bid-ask spread in the tradeable zone < 30¢ (not a 0.001/0.999 ghost book)
    
    Lesson: Backpack FDV NO had $36k 'liquidity' on gamma but bids only at
    0.001–0.05 and asks only at 0.95–0.999. Nothing tradeable in the middle.
    """
    try:
        r = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=5)
        book = r.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        
        # Filter to the tradeable zone (10¢–90¢)
        real_bids = [b for b in bids if 0.10 <= float(b["price"]) <= 0.90]
        real_asks = [a for a in asks if 0.10 <= float(a["price"]) <= 0.90]
        
        # Must have orders on BOTH sides in the tradeable zone
        if len(real_bids) < 2 or len(real_asks) < 2:
            return False
        
        # Spread between best bid and best ask in tradeable zone must be reasonable
        best_bid = max(float(b["price"]) for b in real_bids)
        best_ask = min(float(a["price"]) for a in real_asks)
        spread = best_ask - best_bid
        if spread > 0.30:  # >30¢ spread = no real market maker activity
            return False
        
        # Total depth check
        if side == "YES":
            depth = sum(float(a["size"]) * float(a["price"]) for a in real_asks)
        else:
            depth = sum(float(b["size"]) * float(b["price"]) for b in real_bids)
        
        return depth >= min_depth
    except:
        return False

def place_trade(client, token_id, side, size, neg_risk, tick, yes_price):
    """Place a market order."""
    from py_clob_client.clob_types import (OrderArgs, OrderType,
        PartialCreateOrderOptions, BalanceAllowanceParams, AssetType)
    from py_clob_client.order_builder.constants import BUY, SELL
    
    try:
        # Approve conditional token
        try:
            client.update_balance_allowance(params=BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=2))
        except: pass
        
        # Get current price
        r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=5)
        mid = float(r.json().get("mid", yes_price))
        
        tick_f = float(tick)
        tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        price = round(round(mid / tick_f) * tick_f, tick_dec)
        price = max(0.02, min(0.98, price))
        
        # Size in shares = USDC / price
        shares = math.floor((size / price) * 100) / 100
        if shares < 1:
            return False, "Too few shares"
        
        opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        signed = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)
        
        if receipt.get("success"):
            return True, f"{shares:.1f} shares @ {price:.3f} = ${shares*price:.2f}"
        else:
            return False, receipt.get("errorMsg","unknown")
    except Exception as e:
        return False, str(e)[:100]

def main():
    log("=== Opportunity Scanner Starting ===")
    
    # Get cash
    try:
        usdc, client = get_usdc_balance()
    except Exception as e:
        log(f"Failed to get balance: {e}")
        tg(f"⚠️ <b>Scan failed</b>: balance check error — {e}")
        return
    
    log(f"Available USDC: ${usdc:.2f}")
    
    if usdc < MIN_TRADE_SIZE:
        log(f"Insufficient cash (${usdc:.2f}) — skipping scan")
        return
    
    # Load UW whale/insider signals
    uw_signals = get_uw_signals()
    
    # Get existing positions
    existing_conditions = get_existing_positions()
    log(f"Existing positions: {len(existing_conditions)}")
    
    # Get candidates
    candidates = get_candidate_markets()
    log(f"Scanning {len(candidates)} candidate markets...")
    
    # Skip markets we're already in
    candidates = [c for c in candidates if c["conditionId"] not in existing_conditions]
    log(f"After filtering existing: {len(candidates)} candidates")
    
    trades_placed = []
    skipped = []
    cash_remaining = usdc
    
    for mkt in candidates[:20]:
        q = mkt["question"]
        yes_p = mkt["yes_p"]
        
        # ════════════════════════════════════════════════════════════════
        # TIER 3 — EYES: Rule-based pre-filter (no LLM, no API calls)
        # Inspired by 3-tier model routing: cheap checks first, expensive last.
        # Skip obvious non-opportunities instantly, saving ~40% of Claude calls.
        # ════════════════════════════════════════════════════════════════
        
        _pre_pass = True
        _pre_reason = ""
        
        # Rule 1: Coin-flip markets with no signal — skip (0.42–0.58 yes_p range)
        # These require genuine insight to trade; without UW signal they're noise.
        if 0.42 <= yes_p <= 0.58:
            # Only proceed if UW already has signal OR it's a high-priority category
            _has_preload_signal = any(
                t in (mkt.get("clob_token_ids") or []) 
                for t in (uw_signals or {})
            )
            if not _has_preload_signal:
                _pre_pass = False
                _pre_reason = f"coin-flip ({yes_p:.2f}) no signal"
        
        # Rule 2: Near-certain markets (>92¢ or <8¢) — skip (already handled by get_candidate_markets
        # but re-check here since UW-boosted markets may slip through)
        if yes_p > 0.93 or yes_p < 0.07:
            _pre_pass = False
            _pre_reason = f"near-certain ({yes_p:.2f}) — no edge"
        
        # Rule 3: Short-duration YES on conflict/event markets — blocked (earlier rule)
        # Already applied below, but catch it early to avoid news fetch
        _end_pre = mkt.get("endDate", "")
        _days_pre = 999
        if _end_pre:
            try:
                _edt = datetime.datetime.fromisoformat(_end_pre.replace("Z",""))
                _days_pre = (_edt - datetime.datetime.utcnow()).days
            except: pass
        
        _q_pre = q.lower()
        _conflict_keywords = [
            "ceasefire", "forces enter", "regime fall", "conflict ends",
            "military operations", "invasion", "invade", "war ends", "peace deal",
            "strikes end", "bombing", "kharg", "hormuz", "nuclear deal",
            "attack iran", "bomb iran", "invades iran", "us invade"
        ]
        _is_conflict_pre = any(x in _q_pre for x in _conflict_keywords)
        if _days_pre < 30 and _is_conflict_pre and yes_p > 0.15:
            # YES side of a near-term conflict market — almost always loses
            # Exception: only allow if price is very low (<15¢) — cheap lottery  
            _pre_pass = False
            _pre_reason = f"short-duration conflict ({_days_pre}d) at {yes_p:.2f}"
        
        # Rule 4: Sports markets without strong data signal — skip in off-hours
        import datetime as _dt2
        _hour_utc = _dt2.datetime.utcnow().hour
        _is_sports_pre = any(x in _q_pre for x in [
            "vs.", "vs ", "match", "game", "championship", "tournament",
            "playoff", "cup", "league", "bowl", "series"
        ])
        if _is_sports_pre and not uw_sig:
            # Sports markets without whale flow are coin-flips for us
            _pre_pass = False
            _pre_reason = "sports market, no UW signal"
        
        if not _pre_pass:
            log(f"  [T3-SKIP] {q[:50]} — {_pre_reason}")
            skipped.append({"q": q[:50], "action": "PASS", "edge": 0, 
                           "skip_reason": f"pre-filter: {_pre_reason}"})
            continue
        # ════════════════════════════════════════════════════════════════
        # End Tier 3 pre-filter
        # ════════════════════════════════════════════════════════════════
        
        # Get UW signal
        uw_sig, uw_action_hint = get_uw_signal_for_market(mkt, uw_signals)
        uw_summary_text = uw_signal_summary(uw_sig)
        uw_tags = uw_sig.get("tags", []) if uw_sig else []
        has_insider = any(t in uw_tags for t in ["insider_trades", "contrarian_whales", "whale_position"])
        if uw_sig:
            log(f"  [UW] {q[:40]}: tags={uw_tags[:3]} vol=${uw_sig.get('smart_volume',0):,.0f}")
        
        # Get news
        snippets = fetch_news_snippets(q)
        
        # Score with Claude
        action, edge, reasoning = score_with_claude(q, yes_p, mkt["description"], snippets, uw_summary_text)
        
        # Lower threshold when UW insider/whale signal present
        effective_threshold = MIN_SCAN_EDGE * (1 - UW_EDGE_DISCOUNT) if has_insider else MIN_SCAN_EDGE
        
        # UW override: if Claude PASSed but UW has strong signal and near-miss edge
        if action == "PASS" and uw_action_hint and has_insider and edge >= MIN_SCAN_EDGE * 0.5:
            log(f"  [UW override] Claude PASS → using UW hint {uw_action_hint}")
            action = uw_action_hint
            edge = max(edge, effective_threshold)
        
        # ── Risk guardrail: no BUY_YES on short-duration conflict/event markets ──
        # Lesson: near-term YES bets on Iran/ceasefire/forces events bleed money.
        # Status quo bias: "nothing happens by deadline" wins far more than "something happens."
        # Only allow BUY_YES if: duration > 30 days OR market is not an event/deadline type.
        end_date = mkt.get("endDate", "")
        days_left = 999
        if end_date:
            try:
                end_dt = datetime.datetime.fromisoformat(end_date.replace("Z",""))
                days_left = (end_dt - datetime.datetime.utcnow()).days
            except: pass
        
        q_lower_check = q.lower()
        is_event_market = any(x in q_lower_check for x in [
            "ceasefire", "forces enter", "regime fall", "conflict ends",
            "military operations", "invasion", "invade", "war ends", "peace deal",
            "strikes end", "bombing", "kharg", "hormuz", "attack iran", "bomb iran",
            "us invade", "invades iran", "occupy iran"
        ])
        
        if action == "BUY_YES" and is_event_market and days_left < 30:
            log(f"  SKIP (short-duration YES on event market, {days_left}d left): {q[:45]}")
            skipped.append({"q": q[:50], "action": action, "edge": edge, 
                           "skip_reason": f"event YES <30d ({days_left}d)"})
            continue
        
        log(f"  {q[:50]}: {action} edge={edge:.2f} (thresh={effective_threshold:.2f}) — {reasoning[:55]}")
        
        if action == "PASS" or edge < effective_threshold:
            skipped.append({"q": q[:50], "action": action, "edge": edge})
            continue
        
        # Determine which token to buy
        tokens = mkt["clob_token_ids"]
        if not tokens or len(tokens) < 2:
            log(f"  No token IDs found for {q[:40]}")
            continue
        
        yes_token, no_token = tokens[0], tokens[1]
        buy_token = yes_token if action == "BUY_YES" else no_token
        buy_price = yes_p if action == "BUY_YES" else mkt["no_p"]
        
        # Check real CLOB liquidity
        if not has_real_liquidity(buy_token, min_depth=2000):
            log(f"  Insufficient CLOB depth for {q[:40]} — skip")
            skipped.append({"q": q[:50], "action": action, "edge": edge, "skip_reason": "no_depth"})
            continue
        
        # Get tick size and neg_risk
        try:
            tick = client.get_tick_size(buy_token)
            neg_risk = client.get_neg_risk(buy_token)
        except:
            tick = "0.01"
            neg_risk = False
        
        # Determine trade size
        trade_size = min(MAX_TRADE_SIZE, cash_remaining * 0.3)  # max 30% of remaining cash
        trade_size = max(MIN_TRADE_SIZE, trade_size)
        if trade_size > cash_remaining:
            log(f"  Insufficient cash for {q[:40]}")
            break
        
        # Place the trade
        log(f"  PLACING {action} ${trade_size:.0f} on: {q[:50]}")
        success, detail = place_trade(client, buy_token, action, trade_size,
                                      neg_risk, tick, buy_price)
        
        if success:
            cash_remaining -= trade_size
            trades_placed.append({
                "q": q[:60],
                "action": action,
                "size": trade_size,
                "edge": edge,
                "reasoning": reasoning,
                "detail": detail,
                "uw_signal": bool(uw_sig),
                "uw_tags": uw_tags[:3],
            })
            log(f"  ✓ Trade placed: {detail}")
            time.sleep(2)  # brief pause between trades
        else:
            log(f"  ✗ Trade failed: {detail}")
            skipped.append({"q": q[:50], "action": action, "edge": edge, "skip_reason": detail[:50]})
        
        if cash_remaining < MIN_TRADE_SIZE:
            break
    
    # Build Telegram summary
    now = datetime.datetime.utcnow().strftime("%H:%M UTC")
    
    if trades_placed:
        trade_lines = "\n".join(
            f"• <b>{t['action']}</b> ${t['size']:.0f} | edge={t['edge']:.2f} | {t['q'][:45]}\n"
            f"  ↳ {t['reasoning'][:70]}"
            + (f"\n  🐋 UW: {','.join(t.get('uw_tags', []))[:40]}" if t.get('uw_signal') else "")
            for t in trades_placed
        )
        msg = (f"🤖 <b>Opportunity Scanner</b> [{now}]\n\n"
               f"💰 Cash deployed: ${sum(t['size'] for t in trades_placed):.0f} in {len(trades_placed)} trade(s)\n\n"
               f"{trade_lines}\n\n"
               f"Remaining cash: ${cash_remaining:.0f}")
        tg(msg)
        log(f"Scan complete: {len(trades_placed)} trades placed")
    else:
        # Silent if nothing — don't spam
        log(f"Scan complete: no trades. {len(skipped)} markets evaluated.")
        # Only notify if we had near-misses
        near = [s for s in skipped if s.get("edge",0) >= MIN_SCAN_EDGE * 0.7]
        if near:
            near_lines = "\n".join(f"• {s['q'][:45]} — edge {s.get('edge',0)*100:.0f}% ({s.get('skip_reason','no depth')})"
                                   for s in near[:4])
            tg(f"📊 <b>Scanner [{now}]</b>: no trades placed\n"
               f"Near-misses (blocked by depth/size):\n{near_lines}\n"
               f"Cash available: ${cash_remaining:.0f}")

if __name__ == "__main__":
    main()

