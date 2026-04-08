"""
sell_position_impl.py
Invoked by executor: python3 sell_position_impl.py <token_id> <shares>
Sells the given shares at market price (GTC limit at tick-floored mid).
"""
import sys, os, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')
from py_clob_client.client import ClobClient
from py_clob_client.order_builder.constants import SELL
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions as PCO

if len(sys.argv) < 3:
    print("Usage: sell_position_impl.py <token_id> <shares>")
    sys.exit(1)

TOKEN  = sys.argv[1]
SHARES = float(sys.argv[2])
KEY    = os.environ['POLYMARKET_PRIVATE_KEY']
FND    = os.environ['POLYMARKET_FUNDER_ADDRESS']

client = ClobClient("https://clob.polymarket.com", key=KEY, chain_id=137,
                    signature_type=2, funder=FND)
client.set_api_creds(client.create_or_derive_api_creds())

mid  = float(requests.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN}", timeout=8)
             .json().get("mid", 0))
tick = float(requests.get(f"https://clob.polymarket.com/tick-size?token_id={TOKEN}", timeout=8)
             .json().get("minimum_tick_size", 0.01))

# Floor to nearest valid tick
price = round((mid // tick) * tick, 6)
price = max(tick, price)

print(f"sell_position: {SHARES:.0f}sh @ {price:.4f} (mid={mid:.4f}, tick={tick})")

resp = client.post_order(
    client.create_order(
        OrderArgs(token_id=TOKEN, price=price, size=SHARES, side=SELL),
        PCO(tick_size=tick, neg_risk=False)
    ),
    OrderType.GTC
)
print(f"result: {resp}")
if resp and resp.get("success"):
    print("SOLD OK")
    sys.exit(0)
else:
    print(f"FAILED: {resp}")
    sys.exit(1)
