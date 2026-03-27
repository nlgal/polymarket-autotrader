"""
Emergency sell: BTC $65k dip NO position
Lesson 11: bought when BTC was 1.8% above trigger. Exit immediately.
Asset ID (NO token): 64087619211543545431479218048939484178441767712621033463416084593776314629222
"""
import os, sys, time
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON

KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
SIG_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))
FUNDER = os.environ["POLYMARKET_FUNDER_ADDRESS"]

client = ClobClient(
    "https://clob.polymarket.com",
    key=KEY,
    chain_id=POLYGON,
    signature_type=SIG_TYPE,
    funder=FUNDER,
)
client.set_api_creds(client.create_or_derive_api_creds())

NO_TOKEN = "64087619211543545431479218048939484178441767712621033463416084593776314629222"

# Get current position size
import requests
r = requests.get(f"https://data-api.polymarket.com/positions?user={FUNDER}&limit=50", timeout=15)
positions = r.json()
btc65_pos = [p for p in positions if "65,000" in p.get("title","") or "65000" in p.get("title","")]
if not btc65_pos:
    print("No BTC $65k position found — already sold or never bought")
    exit(0)

pos = btc65_pos[0]
shares = float(pos.get("size", pos.get("currentSize", 0)))
val = float(pos.get("currentValue", 0))
print(f"Position: {shares:.1f} NO shares, current value ${val:.2f}")

if shares <= 0:
    print("No shares to sell")
    exit(0)

# Get best bid
book = client.get_order_book(NO_TOKEN)
bids = sorted(book.bids or [], key=lambda x: float(x.price), reverse=True)
if not bids:
    print("No bids — market illiquid")
    exit(1)
best_bid = float(bids[0].price)
print(f"Best bid: {best_bid:.4f} → expected proceeds: ${shares * best_bid:.2f}")

# Sell at market
order = client.create_market_order(OrderArgs(
    token_id=NO_TOKEN,
    price=best_bid * 0.95,  # 5% below best bid to ensure fill
    size=round(shares, 2),
    side="SELL",
))
resp = client.post_order(order, OrderType.FOK)
print(f"Sell result: {resp}")
