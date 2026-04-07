"""
book_imbalance_scanner.py
=========================
Type-2 "Order Book Imbalance" bot — vague-sourdough style ($1,838 → +$409,980, 223x).

Core insight: Most market participants look at MID PRICE only. 
The order book tells a different story — when one side has 10x more depth,
the thin side is about to be repriced. Enter the THIN side before it moves.

Why this works on geopolitical markets:
- Books are updated slowly by human MMs (not algos)
- News-driven markets create persistent imbalances for hours, not seconds
- 0% fee means no maker rebate competition eating our edge
- We have $200+ minimums so we need big moves — these deliver them

Detection algorithm:
  bid_depth  = sum of (size × price) for top-N bids   (BUY pressure)
  ask_depth  = sum of (size × price) for top-N asks    (SELL pressure)
  
  imbalance_ratio = bid_depth / ask_depth

  ratio > BULL_THRESHOLD → bids dominate → price going UP → BUY YES
  ratio < BEAR_THRESHOLD → asks dominate → price going DOWN → BUY NO

Secondary filter (Claude):
  Imbalance alone is not enough. We require Claude to confirm the imbalance
  has a fundamental news basis — prevents trading into wash trades.

Runs as part of opportunity_scanner.py (called as a scan pass).
Can also run standalone every 30 min via cron.
"""
import os, sys, json, time, datetime, requests, math
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

# ── Config ─────────────────────────────────────────────────────────────────────
PRIVATE_KEY  = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER       = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT      = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# Imbalance thresholds — tuned for geopolitical markets with thin books
BULL_IMBALANCE_RATIO  = 4.0   # bid_depth / ask_depth > 4x → bids dominating → BUY YES
BEAR_IMBALANCE_RATIO  = 0.25  # bid_depth / ask_depth < 0.25x → asks dominating → BUY NO
BOOK_DEPTH_LEVELS     = 10    # Use top-10 levels for depth calculation
MIN_BOOK_DEPTH_USDC   = 500   # Skip if total book depth < $500 (too thin to measure)
MIN_LIQUIDITY         = 25000 # $25k minimum market liquidity
MIN_TRADE_SIZE        = 100   # $100 minimum per trade
MAX_TRADE_SIZE        = 300   # $300 maximum per trade
BUFFER_CASH           = 200   # Always keep $200 in account
STATE_FILE            = "/opt/polymarket-agent/book_imbalance_state.json"
LOG_FILE              = "/opt/polymarket-agent/book_imbalance_scanner.log"

# Cooldown: don't re-enter same market within 4 hours
COOLDOWN_HOURS = 4

_state: dict = {}

def load_state():
    global _state
    try:
        with open(STATE_FILE) as f:
            _state = json.load(f)
    except:
        _state = {}

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(_state, f, indent=2)

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
                timeout=10
            )
        except:
            pass

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [BookImb] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass

def get_book_depth(token_id: str) -> dict:
    """
    Fetch full order book for a token and compute imbalance metrics.
    
    Returns:
        {
          bid_depth: float,   # total $ value of bids (buy pressure)
          ask_depth: float,   # total $ value of asks (sell pressure)
          ratio: float,       # bid_depth / ask_depth
          best_bid: float,    # highest bid price
          best_ask: float,    # lowest ask price
          spread: float,      # best_ask - best_bid
          mid: float,         # (best_bid + best_ask) / 2
          bid_levels: int,    # number of bid price levels
          ask_levels: int,    # number of ask price levels
          thin_side: str,     # "YES" (asks thin) or "NO" (bids thin) or "BALANCED"
        }
    """
    try:
        r = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=8
        )
        if not r.ok:
            return {}
        book = r.json()
    except:
        return {}

    bids = book.get("bids", [])
    asks = book.get("asks", [])

    def depth(orders, n=BOOK_DEPTH_LEVELS):
        total = 0.0
        for o in orders[:n]:
            try:
                total += float(o.get("price", 0)) * float(o.get("size", 0))
            except:
                pass
        return total

    bid_depth = depth(bids)
    ask_depth = depth(asks)

    if bid_depth + ask_depth < MIN_BOOK_DEPTH_USDC:
        return {}  # too thin — no signal

    best_bid = float(bids[0].get("price", 0)) if bids else 0
    best_ask = float(asks[0].get("price", 0)) if asks else 1

    ratio = bid_depth / ask_depth if ask_depth > 0 else 999
    mid   = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0
    spread = best_ask - best_bid if (best_bid and best_ask) else 1

    # Determine thin side
    if ratio > BULL_IMBALANCE_RATIO:
        thin_side = "NO"   # asks (sell side) is thin vs bids → price going UP → buy YES
    elif ratio < BEAR_IMBALANCE_RATIO:
        thin_side = "YES"  # bids (buy side) is thin vs asks → price going DOWN → buy NO
    else:
        thin_side = "BALANCED"

    return {
        "bid_depth":  bid_depth,
        "ask_depth":  ask_depth,
        "ratio":      ratio,
        "best_bid":   best_bid,
        "best_ask":   best_ask,
        "spread":     spread,
        "mid":        mid,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "thin_side":  thin_side,
    }

def claude_confirm_imbalance(question: str, thin_side: str, ratio: float,
                              bid_depth: float, ask_depth: float) -> tuple:
    """
    Ask Claude whether the book imbalance has a fundamental basis.
    Returns (confirmed: bool, reasoning: str)
    
    We don't want to trade purely mechanical imbalances — we want ones
    where the book structure reflects genuine information asymmetry.
    """
    if not ANTHROPIC_KEY:
        return True, "no API key — proceeding without Claude confirmation"

    direction = "BID-HEAVY (buyers dominating)" if ratio > 1 else "ASK-HEAVY (sellers dominating)"
    buy_side  = "YES" if ratio > 1 else "NO"

    prompt = f"""A Polymarket prediction market has a significant order book imbalance:

Market: {question}
Imbalance: {direction}
Ratio: {ratio:.1f}x (bid depth ${bid_depth:.0f} vs ask depth ${ask_depth:.0f})
Signal: BUY {buy_side} (enter the thin side, expect price to move toward heavy side)

Should we trade this imbalance? Consider:
1. Is this likely genuine informed buying/selling vs. a single whale creating fake depth?
2. Does the direction make sense given the market topic?
3. Is there a plausible reason smart money is one-sided here?

Respond ONLY with JSON: {{"trade": true|false, "confidence": 0.0-1.0, "reason": "one sentence"}}
Only confirm (trade=true) if confidence >= 0.65 and the imbalance seems genuinely informative."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15
        )
        text = resp.json().get("content", [{}])[0].get("text", "")
        import re
        match = re.search(r'\{.*?\}', text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            trade      = result.get("trade", False)
            confidence = float(result.get("confidence", 0))
            reason     = result.get("reason", "")
            return (trade and confidence >= 0.65), reason
    except Exception as e:
        log(f"  Claude confirm error: {e}")

    return False, "Claude confirmation failed"

def get_candidate_markets_with_depth():
    """
    Fetch active markets and compute book depth for each.
    Returns candidates sorted by imbalance strength.
    """
    candidates = []

    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets"
            "?active=true&closed=false&limit=100&order=volume24hr&ascending=false",
            timeout=15
        )
        if not r.ok:
            return []
        markets = r.json()
    except Exception as e:
        log(f"Market fetch error: {e}")
        return []

    now = datetime.datetime.utcnow()

    for m in markets:
        liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        if liq < MIN_LIQUIDITY:
            continue

        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
            yes_p  = float(prices[0])
        except:
            continue

        # Skip near-certain markets (near-res scanner handles those)
        if yes_p > 0.94 or yes_p < 0.06:
            continue

        # Skip markets resolving very soon
        end_str = m.get("endDate", "") or ""
        if end_str:
            try:
                end_dt = datetime.datetime.fromisoformat(end_str.replace("Z", ""))
                if (end_dt - now).total_seconds() < 1800:
                    continue
            except:
                pass

        q = m.get("question", "")
        q_lower = q.lower()

        # Skip clearly irrelevant market types
        if any(x in q_lower for x in ["how many tweets", "post from", "fdv", "tge"]):
            continue

        cid = m.get("conditionId", "")
        try:
            tokens = json.loads(m.get("clobTokenIds", "[]") or "[]")
        except:
            tokens = []

        if not tokens:
            continue

        yes_token = tokens[0] if len(tokens) > 0 else ""
        no_token  = tokens[1] if len(tokens) > 1 else ""

        if not yes_token:
            continue

        # Fetch book depth for YES token
        depth_data = get_book_depth(yes_token)
        if not depth_data or depth_data.get("thin_side") == "BALANCED":
            continue

        ratio      = depth_data.get("ratio", 1.0)
        thin_side  = depth_data.get("thin_side", "BALANCED")
        spread     = depth_data.get("spread", 1.0)

        # Skip if spread is very wide (illiquid / stale book)
        if spread > 0.15:
            log(f"  SKIP (wide spread {spread:.3f}): {q[:50]}")
            continue

        # Determine which token to buy
        if thin_side == "NO":
            # Asks are thin → price going up → BUY YES
            action   = "BUY_YES"
            token_id = yes_token
            price    = depth_data.get("best_ask", yes_p)
        else:
            # Bids are thin → price going down → BUY NO
            action   = "BUY_NO"
            token_id = no_token
            price    = 1 - depth_data.get("best_bid", yes_p)

        # Cooldown check
        cooldown_key = f"{cid}:{action}"
        if cooldown_key in _state:
            last_ts = _state[cooldown_key].get("ts", 0)
            if time.time() - last_ts < COOLDOWN_HOURS * 3600:
                continue

        candidates.append({
            "question":   q,
            "conditionId": cid,
            "yes_p":      yes_p,
            "action":     action,
            "token_id":   token_id,
            "price":      price,
            "ratio":      ratio,
            "thin_side":  thin_side,
            "bid_depth":  depth_data.get("bid_depth", 0),
            "ask_depth":  depth_data.get("ask_depth", 0),
            "spread":     spread,
            "mid":        depth_data.get("mid", yes_p),
            "liq":        liq,
            "vol24h":     float(m.get("volume24hr") or 0),
            "end_str":    end_str,
        })

    # Sort by strength of imbalance
    def imbalance_strength(c):
        r = c["ratio"]
        # Distance from 1.0 (balanced) in log scale
        return abs(math.log(r)) if r > 0 else 0

    candidates.sort(key=imbalance_strength, reverse=True)
    return candidates

def place_imbalance_trade(candidate, usdc_available, client):
    """Place trade on the imbalanced side."""
    from py_clob_client.clob_types import MarketOrderArgs, OrderType

    action   = candidate["action"]
    token_id = candidate["token_id"]
    question = candidate["question"]
    price    = candidate["price"]
    ratio    = candidate["ratio"]

    size_usdc = min(MAX_TRADE_SIZE, max(MIN_TRADE_SIZE, usdc_available * 0.08))

    if PAPER_TRADE_ONLY:
        log(f"  [PAPER] Would place: {action} '{question[:50]}' @ {price:.4f} (ratio={ratio:.1f}x) — paper trade only")
        tg(f"📋 <b>[PAPER TRADE]</b> Book Imbalance signal (not executed):\n"
           f"{action} '{question[:55]}'\n"
           f"Price: {price:.4f} | Ratio: {ratio:.1f}x | Would deploy: ${min(MAX_TRADE_SIZE, 100):.0f}")
        return True, min(MAX_TRADE_SIZE, 100)  # simulate fill for tracking

    log(f"  PLACING: {action} '{question[:50]}' @ {price:.4f} (ratio={ratio:.1f}x)")
    log(f"  Size: ${size_usdc:.2f} | bid_depth=${candidate['bid_depth']:.0f} ask_depth=${candidate['ask_depth']:.0f}")

    try:
        order = client.create_market_order(MarketOrderArgs(
            token_id=token_id,
            amount=size_usdc,
        ))
        resp = client.post_order(order, OrderType.FOK)

        if resp and resp.get("success"):
            log(f"  ✅ FILLED: {action} '{question[:40]}' for ${size_usdc:.2f}")
            direction = "📈 BUY YES" if action == "BUY_YES" else "📉 BUY NO"
            tg(
                f"📚 <b>Book Imbalance Trade</b>\n"
                f"{direction}\n"
                f"Market: {question[:60]}\n"
                f"Price: {price:.4f} | Imbalance: {ratio:.1f}x\n"
                f"Bid depth: ${candidate['bid_depth']:.0f} | Ask depth: ${candidate['ask_depth']:.0f}\n"
                f"Size: ${size_usdc:.2f}"
            )
            return True, size_usdc
        else:
            log(f"  ❌ Order failed: {resp}")
            return False, 0

    except Exception as e:
        log(f"  ❌ Exception: {e}")
        return False, 0

def run():
    log("=== Book Imbalance Scanner starting ===")
    load_state()

    # Get USDC balance
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                            chain_id=137, signature_type=2, funder=FUNDER)
        c = client.create_or_derive_api_creds()
        client.set_api_creds(c)
        bal = client.get_balance_allowance(params=BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=2))
        usdc_balance = float(bal.get("balance", 0)) / 1e6
    except Exception as e:
        log(f"Balance fetch error: {e} — exiting")
        return

    log(f"USDC balance: ${usdc_balance:.2f}")
    usdc_available = max(0, usdc_balance - BUFFER_CASH)

    if usdc_available < MIN_TRADE_SIZE:
        log(f"Insufficient balance after buffer (${usdc_available:.2f}) — exiting")
        return

    candidates = get_candidate_markets_with_depth()
    log(f"Imbalanced markets found: {len(candidates)}")

    trades_placed = 0
    usdc_spent    = 0
    MAX_PER_RUN   = 600  # $600 max deployment per scanner run

    for c in candidates:
        if usdc_spent >= MAX_PER_RUN:
            break
        if usdc_available - usdc_spent < MIN_TRADE_SIZE:
            break

        log(f"\nImbalance: '{c['question'][:60]}'")
        log(f"  Action={c['action']} ratio={c['ratio']:.1f}x spread={c['spread']:.3f}")

        # Claude confirmation (prevents trading manipulated books)
        confirmed, reason = claude_confirm_imbalance(
            c["question"], c["thin_side"], c["ratio"],
            c["bid_depth"], c["ask_depth"]
        )
        log(f"  Claude: {'✅' if confirmed else '❌'} {reason}")

        if not confirmed:
            continue

        remaining  = usdc_available - usdc_spent
        filled, spent = place_imbalance_trade(c, remaining, client)

        if filled:
            trades_placed += 1
            usdc_spent    += spent
            cooldown_key  = f"{c['conditionId']}:{c['action']}"
            _state[cooldown_key] = {
                "ts":       time.time(),
                "question": c["question"][:60],
                "ratio":    c["ratio"],
                "spent":    spent,
            }
            save_state()

    log(f"\n=== Done: {trades_placed} trades, ${usdc_spent:.2f} deployed ===")

def scan_only() -> list:
    """
    Scan-only mode: returns imbalance candidates without placing trades.
    Used when called from opportunity_scanner.py as a scan pass.
    """
    load_state()
    return get_candidate_markets_with_depth()

if __name__ == "__main__":
    run()
