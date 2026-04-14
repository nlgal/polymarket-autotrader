
import os, sys, json, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

HOST    = "https://clob.polymarket.com"
KEY     = os.environ["PRIVATE_KEY"]
FUNDER  = os.environ["POLYMARKET_FUNDER_ADDRESS"]
CHAIN_ID = 137

TOKEN   = "31869663470674113137413574637244458645080626094263106105850314096988098095887"
USDC_AMOUNT = 400.0   # dollars to spend

client = ClobClient(HOST, key=KEY, chain_id=CHAIN_ID, signature_type=2, funder=FUNDER)

# Get current mid and tick
mid_r  = requests.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN}", timeout=8)
tick_r = requests.get(f"https://clob.polymarket.com/tick-size?token_id={TOKEN}", timeout=8)
mid    = float(mid_r.json().get("mid", 0.235))
tick   = float(tick_r.json().get("minimum_tick_size", 0.01))

# Round price up one tick to ensure fill
n      = round(mid / tick)
price  = round((n + 1) * tick, 6)
price  = min(price, 0.99)

# Shares = USDC / price
shares = round(USDC_AMOUNT / price, 2)

print(f"BUY May15 NO: {shares:.2f}sh @ {price:.3f} = ${shares * price:.2f}")

try:
    order = client.create_order(
        OrderArgs(token_id=TOKEN, price=price, size=shares, side=BUY),
        PartialCreateOrderOptions(tick_size=tick, neg_risk=False)
    )
    resp = client.post_order(order, OrderType.GTC)
    if resp and resp.get("success"):
        print(f"SUCCESS: order_id={resp.get('orderID','')}")
        print(f"Bought {shares:.2f}sh of Iran May15 NO @ {price:.3f}")
    else:
        print(f"FAILED: {resp}")
        # Try without neg_risk
        order2 = client.create_order(
            OrderArgs(token_id=TOKEN, price=price, size=shares, side=BUY),
            PartialCreateOrderOptions(tick_size=tick)
        )
        resp2 = client.post_order(order2, OrderType.GTC)
        print(f"Retry: {resp2}")
except Exception as e:
    import traceback
    print(f"Error: {e}")
    traceback.print_exc()
