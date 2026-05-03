"""
sell_aitc_no.py — Direct sell for AITC NO contradiction exit
104 shares of AITC West Bengal NO token
Token: 106042778569020456846448290057936385726097394899443862594454878965392595546558
"""
import os, sys, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderType
from py_clob_client_v2.order_builder.constants import SELL

KEY      = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER   = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
SIG_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))

# AITC NO token
TOKEN  = "106042778569020456846448290057936385726097394899443862594454878965392595546558"
SHARES = 104.0

client = ClobClient("https://clob.polymarket.com", key=KEY, chain_id=137,
                    signature_type=SIG_TYPE, funder=FUNDER or None)
try:    creds = client.create_or_derive_api_key()
except: creds = client.derive_api_key()
client.set_api_creds(creds)

# Get current mid price — try SDK first, fall back to REST
try:
    mid = float(client.get_midpoint(TOKEN).get("mid", 0.50))
except Exception:
    try:
        mid_r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN}", timeout=8)
        mid   = float(mid_r.json().get("mid", 0.50))
    except Exception:
        mid = 0.50  # fallback: sell at mid if API unavailable

try:
    tick = float(client.get_tick_size(TOKEN))
except Exception:
    try:
        tick_r = requests.get(f"https://clob.polymarket.com/tick-size?token_id={TOKEN}", timeout=8)
        tick   = float(tick_r.json().get("minimum_tick_size", 0.01))
    except Exception:
        tick = 0.01  # default tick size

# Sell just below mid to get filled quickly (1 tick below)
price = max(round((round(mid / tick) - 1) * tick, 6), 0.01)

print(f"SELL AITC NO: {SHARES:.2f}sh @ {price:.3f} (mid={mid:.3f}) = ${SHARES * price:.2f}")

# Plain create_order — no PartialCreateOrderOptions (avoids neg_risk allowance issue)
order = client.create_order(OrderArgs(token_id=TOKEN, price=price, size=SHARES, side=SELL))
resp  = client.post_order(order, OrderType.GTC)

if resp and resp.get("success"):
    print(f"SUCCESS order_id={resp.get('orderID', '')} | {SHARES:.2f}sh @ {price:.3f} = ${SHARES * price:.2f}")
else:
    print(f"FAILED: {resp}")
    sys.exit(1)
