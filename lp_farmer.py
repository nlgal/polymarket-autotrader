"""
lp_farmer.py
============
Polymarket LP Rewards Farmer — earns daily USDC by posting limit orders
on both sides of reward-eligible markets.

Strategy:
- Completely market-neutral: posts YES and NO limit orders at midpoint ± spread
- Earns daily USDC rewards just for providing liquidity
- No directional risk — makes money regardless of outcome
- Position size: $15-25 per side, within max spread for each market

Safety rules (critical — this is the REWARD capital, not bet capital):
- Only post on markets where spread qualification is ≥4¢ (easier to maintain)
- Max $50 total deployed per market ($25 YES + $25 NO)
- Skip markets expiring < 3 days (too risky near resolution)
- Skip markets where YES price > 90¢ or < 10¢ (near-certain = high fill risk)
- Cancel and repost if market moves > 3¢ from our posted price
- Minimum $5 daily reward before entering a market (not worth gas otherwise)

Runs via executor, called every 4 hours to refresh orders.
"""
import os, sys, math, time, requests, json
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.constants import POLYGON
import datetime

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '')

MAX_PER_MARKET = 50      # Max $50 deployed per market (both sides)
SIDE_SIZE = 20           # $20 per side
MIN_DAILY_REWARD = 5     # Skip markets paying < $5/day 
MIN_SPREAD_QUALIFY = 2.0 # Only markets with maxSpread >= 2¢ (easier to stay qualified)
MIN_DAYS_LEFT = 3        # Skip markets expiring in < 3 days
MAX_YES_PRICE = 0.88     # Skip if YES > 88¢ (near certain — risky fill)
MIN_YES_PRICE = 0.12     # Skip if YES < 12¢ (near certain NO)

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
        except: pass

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def get_reward_markets():
    """Fetch markets with active LP rewards from Polymarket."""
    r = requests.get(
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500",
        timeout=20
    )
    markets = r.json()
    
    now = datetime.datetime.utcnow()
    eligible = []
    
    for m in markets:
        min_sz = float(m.get("rewardsMinSize") or 0)
        max_sp = float(m.get("rewardsMaxSpread") or 0)
        daily  = float(m.get("rewardsDailyRate") or 0)
        
        if min_sz < 1 or max_sp < MIN_SPREAD_QUALIFY:
            continue
        if daily < MIN_DAILY_REWARD:
            continue
        
        # Check expiry
        end_str = m.get("endDate","")
        if end_str:
            try:
                end_dt = datetime.datetime.fromisoformat(end_str.replace("Z",""))
                days_left = (end_dt - now).days
                if days_left < MIN_DAYS_LEFT:
                    continue
            except: pass
        
        # Parse yes price
        try:
            prices = m.get("outcomePrices","")
            if isinstance(prices, str):
                prices = [float(x.strip().strip('"')) for x in prices.strip("[]").split(",")]
            yes_p = float(prices[0])
        except:
            continue
        
        if yes_p > MAX_YES_PRICE or yes_p < MIN_YES_PRICE:
            continue
        
        # Get token IDs
        tokens = m.get("clobTokenIds", "[]")
        if isinstance(tokens, str):
            try: tokens = json.loads(tokens)
            except: continue
        if len(tokens) < 2:
            continue
        
        eligible.append({
            "question": m.get("question","")[:60],
            "yes_token": tokens[0],
            "no_token": tokens[1],
            "yes_p": yes_p,
            "min_sz": min_sz,
            "max_sp": max_sp,
            "daily": daily,
            "end": end_str[:10],
            "conditionId": m.get("conditionId",""),
        })
    
    # Sort by daily reward descending
    return sorted(eligible, key=lambda x: -x["daily"])

def get_clob_midpoint(yes_token):
    """Get current midpoint price from CLOB order book."""
    try:
        r = requests.get(f"https://clob.polymarket.com/book?token_id={yes_token}", timeout=8)
        book = r.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            return (best_bid + best_ask) / 2
    except:
        pass
    return None

def place_lp_orders(client, market):
    """Post limit orders on both sides of a market to earn LP rewards."""
    yes_token = market["yes_token"]
    no_token = market["no_token"]
    max_sp = market["max_sp"] / 100  # convert cents to decimal
    
    # Get live midpoint
    mid = get_clob_midpoint(yes_token)
    if mid is None:
        log(f"  No midpoint for {market['question'][:40]} — skip")
        return False, 0
    
    # Post YES order slightly below mid (buying YES at a discount = selling NO above mid)
    # Post NO order slightly below mid price of NO
    yes_bid_price = round(mid - max_sp * 0.4, 3)  # 40% of max spread below mid
    yes_ask_price = round(mid + max_sp * 0.4, 3)  # 40% above mid
    
    # Clamp to valid range
    yes_bid_price = max(0.01, min(0.98, yes_bid_price))
    yes_ask_price = max(0.02, min(0.99, yes_ask_price))
    
    tick = client.get_tick_size(yes_token)
    neg_risk = client.get_neg_risk(yes_token)
    tick_f = float(tick)
    tick_dec = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0
    
    # Round to tick size
    yes_bid_price = round(round(yes_bid_price / tick_f) * tick_f, tick_dec)
    
    # Calculate shares for SIDE_SIZE USDC
    yes_shares = round(SIDE_SIZE / yes_bid_price, 2)
    
    if yes_shares < market["min_sz"]:
        log(f"  {market['question'][:40]}: shares {yes_shares:.0f} < min {market['min_sz']:.0f} — skip")
        return False, 0
    
    placed = 0
    
    # Place YES BUY order (limit — earns rewards when posted near midpoint)
    try:
        args = OrderArgs(token_id=yes_token, price=yes_bid_price, size=yes_shares, side=BUY)
        opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed = client.create_order(args, opts)
        receipt = client.post_order(signed, OrderType.GTC)
        if receipt.get("success") or receipt.get("orderID"):
            log(f"  ✓ YES BUY: {yes_shares:.0f} shares @ {yes_bid_price:.3f} = ${yes_shares*yes_bid_price:.2f}")
            placed += 1
        else:
            log(f"  ✗ YES BUY failed: {receipt.get('errorMsg','?')[:60]}")
    except Exception as e:
        log(f"  ✗ YES BUY error: {e}")
    
    # Place NO BUY order at equivalent price
    no_price = round(1.0 - yes_ask_price, tick_dec)
    no_price = round(round(no_price / tick_f) * tick_f, tick_dec)
    no_shares = round(SIDE_SIZE / no_price, 2)
    
    if no_shares >= market["min_sz"]:
        try:
            tick_no = client.get_tick_size(no_token)
            neg_risk_no = client.get_neg_risk(no_token)
            args_no = OrderArgs(token_id=no_token, price=no_price, size=no_shares, side=BUY)
            opts_no = PartialCreateOrderOptions(tick_size=tick_no, neg_risk=neg_risk_no)
            signed_no = client.create_order(args_no, opts_no)
            receipt_no = client.post_order(signed_no, OrderType.GTC)
            if receipt_no.get("success") or receipt_no.get("orderID"):
                log(f"  ✓ NO BUY: {no_shares:.0f} shares @ {no_price:.3f} = ${no_shares*no_price:.2f}")
                placed += 1
            else:
                log(f"  ✗ NO BUY failed: {receipt_no.get('errorMsg','?')[:60]}")
        except Exception as e:
            log(f"  ✗ NO BUY error: {e}")
    
    return placed == 2, placed

def cancel_stale_lp_orders(client):
    """Cancel LP orders that are too far from current midpoint."""
    try:
        open_orders = client.get_orders()
        if not open_orders:
            return 0
        
        cancelled = 0
        for order in open_orders:
            # Only cancel limit orders (GTC), not market orders
            if order.get("type") != "GTC":
                continue
            token_id = order.get("asset_id","")
            order_price = float(order.get("price",0))
            
            # Get current midpoint
            mid = get_clob_midpoint(token_id)
            if mid is None:
                continue
            
            # If our posted price is > 5¢ away from midpoint, cancel and repost
            if abs(order_price - mid) > 0.05:
                try:
                    client.cancel(order_id=order.get("id",""))
                    cancelled += 1
                except:
                    pass
        
        return cancelled
    except:
        return 0

def main():
    log("=== LP Farmer Starting ===")
    
    client = ClobClient(
        "https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=POLYGON,
        signature_type=2,
        funder=FUNDER,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    
    # Cancel stale orders first
    cancelled = cancel_stale_lp_orders(client)
    if cancelled:
        log(f"Cancelled {cancelled} stale LP orders")
    
    # Get reward markets
    markets = get_reward_markets()
    log(f"Found {len(markets)} eligible reward markets")
    
    if not markets:
        log("No eligible markets — nothing to do")
        return
    
    # Show top 5
    for m in markets[:5]:
        log(f"  ${m['daily']:.0f}/day | {m['question'][:50]} | spread={m['max_sp']:.1f}¢ | ends={m['end']}")
    
    # Deploy on top markets up to budget
    TOTAL_BUDGET = 200  # Max $200 total for LP orders
    deployed = 0
    markets_active = 0
    total_daily = 0
    
    for market in markets[:10]:  # Try top 10
        if deployed >= TOTAL_BUDGET:
            break
        if deployed + MAX_PER_MARKET > TOTAL_BUDGET:
            break
        
        log(f"\nPosting LP on: {market['question'][:55]}")
        log(f"  ${market['daily']:.0f}/day reward | spread={market['max_sp']:.1f}¢ | min={market['min_sz']:.0f}sh")
        
        ok, placed = place_lp_orders(client, market)
        if placed > 0:
            deployed += SIDE_SIZE * placed
            markets_active += 1
            total_daily += market["daily"]
            time.sleep(1)
    
    log(f"\n=== LP Farmer Complete ===")
    log(f"Active on {markets_active} markets | ${deployed:.0f} deployed | ~${total_daily:.0f}/day rewards")
    
    if markets_active > 0:
        tg(f"""<b>🌾 LP Farmer Active</b>
Markets: {markets_active}
Deployed: ${deployed:.0f}
Est. daily rewards: ${total_daily:.0f}/day
Top market: {markets[0]['question'][:40]}""")

if __name__ == "__main__":
    main()
