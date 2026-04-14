import os, sys, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

KEY      = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER   = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
SIG_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE","2"))
TOKEN    = "31869663470674113137413574637244458645080626094263106105850314096988098095887"
USDC     = 400.0

client = ClobClient("https://clob.polymarket.com", key=KEY, chain_id=137,
                    signature_type=SIG_TYPE, funder=FUNDER or None)
try:    creds = client.create_or_derive_api_creds()
except: creds = client.derive_api_key()
client.set_api_creds(creds)

mid   = float(requests.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN}",timeout=8).json().get("mid",0.235))
tick  = float(requests.get(f"https://clob.polymarket.com/tick-size?token_id={TOKEN}",timeout=8).json().get("minimum_tick_size",0.01))
price = min(round((round(mid/tick)+1)*tick, 6), 0.99)
shares = round(USDC / price, 2)

print(f"BUY Iran May15 NO: {shares:.2f}sh @ {price:.3f} = ${shares*price:.2f} | mid={mid:.3f}")

# Plain create_order (no PartialCreateOrderOptions — avoids neg_risk param issue)
order = client.create_order(OrderArgs(token_id=TOKEN, price=price, size=shares, side=BUY))
resp  = client.post_order(order, OrderType.GTC)
if resp and resp.get("success"):
    print(f"SUCCESS order_id={resp.get('orderID','')} | {shares:.2f}sh @ {price:.3f} = ${shares*price:.2f}")
else:
    print(f"FAILED: {resp}")
    sys.exit(1)
