import os, sys, json, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

HOST     = "https://clob.polymarket.com"
KEY      = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER   = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
SIG_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE","2"))

TOKEN       = "31869663470674113137413574637244458645080626094263106105850314096988098095887"
USDC_AMOUNT = 400.0

client = ClobClient(HOST, key=KEY, chain_id=137, signature_type=SIG_TYPE, funder=FUNDER or None)
try:
    creds = client.create_or_derive_api_creds()
except AttributeError:
    creds = client.derive_api_key()
client.set_api_creds(creds)

mid_r  = requests.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN}", timeout=8)
tick_r = requests.get(f"https://clob.polymarket.com/tick-size?token_id={TOKEN}", timeout=8)
mid    = float(mid_r.json().get("mid", 0.235))
tick   = float(tick_r.json().get("minimum_tick_size", 0.01))

n      = round(mid / tick)
price  = round((n + 1) * tick, 6)
price  = min(price, 0.99)
shares = round(USDC_AMOUNT / price, 2)

print(f"BUY Iran May15 NO: {shares:.2f}sh @ {price:.3f} = ${shares*price:.2f}")
print(f"Market mid: {mid:.3f} | tick: {tick}")

order = None
for kwargs in [
    {"tick_size": tick, "neg_risk": False},
    {"tick_size": tick},
    {},
]:
    try:
        if kwargs:
            order = client.create_order(
                OrderArgs(token_id=TOKEN, price=price, size=shares, side=BUY),
                PartialCreateOrderOptions(**kwargs)
            )
        else:
            order = client.create_order(
                OrderArgs(token_id=TOKEN, price=price, size=shares, side=BUY)
            )
        break
    except Exception as e:
        print(f"  create_order kwargs={kwargs} failed: {e}")
        continue

if order is None:
    print("All create_order attempts failed")
    sys.exit(1)

resp = client.post_order(order, OrderType.GTC)
if resp and resp.get("success"):
    print(f"SUCCESS: {resp.get('orderID','')}")
    print(f"Bought {shares:.2f}sh of Iran May15 NO @ {price:.3f} = ${shares*price:.2f}")
else:
    print(f"FAILED: {resp}")
    sys.exit(1)
