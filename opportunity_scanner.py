"""
opportunity_scanner.py
======================
Autonomous opportunity scanner — runs every 2 hours via cron.
Scans Polymarket for mispriced markets using fresh news research,
places trades directly when edge > threshold, sends Telegram summary.

Strategy:
- Pulls top 50 markets by liquidity/volume
- Skips markets we're already in or recently traded
- For each candidate: fetches recent news via RSS/search, scores edge
- Places BUY_NO or BUY_YES directly via CLOB when edge >= MIN_SCAN_EDGE
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

MIN_SCAN_EDGE    = 0.12   # Higher bar than autotrader's 0.07 — only obvious mispricings
MIN_LIQUIDITY    = 50000  # $50k minimum liquidity
MAX_TRADE_SIZE   = 150    # Max USDC per trade
MIN_TRADE_SIZE   = 50
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
        # Skip in-play sports (spread/totals), tweet counts
        if any(x in q.lower() for x in ["spread:", "o/u", "tweets", "post from", "how many times"]):
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

def score_with_claude(question, yes_p, description, news_snippets):
    """Use Claude to score the edge given fresh news."""
    if not ANTHROPIC_KEY:
        return None, None, "No API key"
    
    prompt = f"""You are a prediction market analyst. Score the following market and determine if there's a trading edge.

MARKET: {question}
CURRENT YES PRICE: {yes_p:.2f} (implies {yes_p*100:.0f}% probability)
DESCRIPTION: {description}

RECENT NEWS CONTEXT:
{news_snippets}

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
- For sports/games with no clear favorite signal, PASS."""
    
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-3-5-haiku-20241022",
                  "max_tokens": 256,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20)
        content = r.json().get("content", [{}])[0].get("text","")
        # Extract JSON
        match = re.search(r'\{.*?\}', content, re.DOTALL)
        if match:
            result = json.loads(match.group())
            return result.get("action","PASS"), result.get("edge",0), result.get("reasoning","")
        return "PASS", 0, content[:100]
    except Exception as e:
        return "PASS", 0, str(e)[:80]

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

def has_real_liquidity(token_id, side="YES", min_depth=1000):
    """Check if the order book has real depth at reasonable prices."""
    try:
        r = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=5)
        book = r.json()
        if side == "YES":
            # For BUY YES: check asks (people selling YES tokens we'd buy)
            asks = book.get("asks", [])
            depth_at_reasonable = sum(float(a["size"]) * float(a["price"])
                for a in asks if float(a["price"]) < 0.98)
        else:
            # For BUY NO: check bids on the NO token or asks on the YES token
            asks = book.get("asks", [])
            depth_at_reasonable = sum(float(a["size"]) * float(a["price"])
                for a in asks if 0.03 < float(a["price"]) < 0.97)
        return depth_at_reasonable >= min_depth
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
        
        # Get news
        snippets = fetch_news_snippets(q)
        
        # Score with Claude
        action, edge, reasoning = score_with_claude(q, yes_p, mkt["description"], snippets)
        log(f"  {q[:50]}: {action} edge={edge:.2f} — {reasoning[:60]}")
        
        if action == "PASS" or edge < MIN_SCAN_EDGE:
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
                "detail": detail
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
            f"• <b>{t['action']}</b> ${t['size']:.0f} | edge={t['edge']:.0f}% | {t['q'][:45]}\n"
            f"  ↳ {t['reasoning'][:70]}"
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
