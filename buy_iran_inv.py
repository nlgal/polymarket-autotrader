import os, sys, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderType
from py_clob_client_v2.order_builder.constants import BUY

KEY      = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER   = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
SIG_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE","2"))
TOKEN    = "55115078421062885512539156303747803058407616201213034911037320915726138659123"
USDC     = 350.0

client = ClobClient("https://clob.polymarket.com", key=KEY, chain_id=137,
                    signature_type=SIG_TYPE, funder=FUNDER or None)
try:    creds = client.create_or_derive_api_key()
except: creds = client.derive_api_key()
client.set_api_creds(creds)

mid   = float(requests.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN}",timeout=8).json().get("mid",0.305))
tick  = float(requests.get(f"https://clob.polymarket.com/tick-size?token_id={TOKEN}",timeout=8).json().get("minimum_tick_size",0.01))
price = min(round((round(mid/tick)+1)*tick,6), 0.99)
shares = round(USDC / price, 2)

print(f"BUY Iran invasion YES: {shares:.2f}sh @ {price:.3f} = ${shares*price:.2f} | mid={mid:.3f}")
order = client.create_order(OrderArgs(token_id=TOKEN, price=price, size=shares, side=BUY))
resp  = client.post_order(order, OrderType.GTC)
if resp and resp.get("success"):
    print(f"SUCCESS: {shares:.2f}sh @ {price:.3f} = ${shares*price:.2f} | id={resp.get('orderID','')[:24]}...")
else:
    print(f"FAILED: {resp}")
    sys.exit(1)
