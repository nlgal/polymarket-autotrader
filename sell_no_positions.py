"""
Emergency sell: exit all ceasefire NO positions
Apr15 NO (400sh), Apr30 NO (~1184sh)
Jun30 NO — HOLD (profitable at 87.5¢)
"""
import os, sys, json, time
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType

PRIVATE_KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
FUNDER      = os.environ["POLYMARKET_FUNDER_ADDRESS"]

client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY,
                    chain_id=137, signature_type=2, funder=FUNDER)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

# Positions to sell: token_id → label
# Selling NO tokens = selling our NO shares back to market
SELLS = [
    {
        "label":    "Apr15 NO",
        "token_id": "8442709013751543525223072638303914942960068246422295030411662679470140144155",
        "shares":   400,
    },
    {
        "label":    "Apr30 NO",
        "token_id": "52284848830940446862370529859386043059769275594386884690262695607365719243018",
        "shares":   1184,
    },
]

import requests
def get_best_bid(token_id):
    r = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=8)
    if r.ok:
        bids = r.json().get("bids", [])
        if bids:
            return float(bids[0]["price"]), float(bids[0]["size"])
    return 0, 0

print("=== EMERGENCY SELL — CEASEFIRE NO POSITIONS ===\n")

total_proceeds = 0

for pos in SELLS:
    label    = pos["label"]
    token_id = pos["token_id"]
    shares   = pos["shares"]

    best_bid, bid_size = get_best_bid(token_id)
    print(f"{label}: {shares}sh | best bid: {best_bid:.4f} ({bid_size:.0f}sh available)")

    if best_bid < 0.01:
        print(f"  ⚠️  No bids — skipping {label}")
        continue

    expected = shares * best_bid
    print(f"  Expected proceeds: ${expected:.2f}")

    try:
        # Use SELL side market order — sell our shares for USDC
        order = client.create_market_order(MarketOrderArgs(
            token_id=token_id,
            amount=shares,
            side="SELL",
        ))
        resp = client.post_order(order, OrderType.FOK)
        print(f"  Result: {resp}")
        if resp and resp.get("success"):
            print(f"  ✅ SOLD {label}")
            total_proceeds += expected
        else:
            print(f"  ❌ Failed: {resp}")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    time.sleep(1)

print(f"\nTotal proceeds: ~${total_proceeds:.2f}")
