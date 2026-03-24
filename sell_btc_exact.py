"""
sell_btc_exact.py — Sell BTC $80k NO using exact balance from CLOB API
Balance: 337238879 base units = 337.238879 shares
"""
import os, sys, requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
SIG_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE","2"))
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN","").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID","").strip()
TOKEN_ID = "96839284769036407740491691016901048240322264125970194871307313464800669089139"
ENTRY = 0.5656

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import SELL

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML"}, timeout=10)
        except: pass

client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY, chain_id=137,
                    creds=ApiCreds(api_key=os.environ.get("CLOB_API_KEY",""),
                                   api_secret=os.environ.get("CLOB_API_SECRET",""),
                                   api_passphrase=os.environ.get("CLOB_API_PASSPHRASE","")),
                    signature_type=SIG_TYPE, funder=FUNDER)
try:
    c = client.create_or_derive_api_creds(); client.set_api_creds(c)
    print("Creds OK")
except Exception as e:
    print(f"Creds: {e}")

# Get EXACT balance from CLOB
bal = client.get_balance_allowance(params=BalanceAllowanceParams(
    asset_type=AssetType.CONDITIONAL, token_id=TOKEN_ID, signature_type=2))
raw_balance = int(bal.get("balance", 0))
# Balance is in 1e6 units (same as USDC) — convert to shares
exact_shares = raw_balance / 1e6
print(f"Exact balance: {raw_balance} units = {exact_shares:.6f} shares")

# Get current mid
r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN_ID}", timeout=8)
mid = float(r.json().get("mid", 0.9)) if r.status_code == 200 else 0.9
print(f"Current mid: {mid:.4f}")

tick = client.get_tick_size(TOKEN_ID)
neg_risk = client.get_neg_risk(TOKEN_ID)
tick_f = float(tick)
tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
sell_price = round(round(mid / tick_f) * tick_f, tick_dec)
sell_price = max(0.01, min(0.99, sell_price))

# Use floor to 2 decimal places to avoid exceeding balance
size = round(exact_shares, 2)
print(f"Selling: {size} shares @ {sell_price:.4f} = ${size*sell_price:.2f}")
print(f"neg_risk={neg_risk} tick={tick}")

args = OrderArgs(token_id=TOKEN_ID, price=sell_price, size=size, side=SELL)
opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
try:
    signed = client.create_order(args, opts)
    receipt = client.post_order(signed, OrderType.GTC)
    if receipt.get("success"):
        proceeds = sell_price * size
        profit = proceeds - ENTRY * size
        print(f"\nSOLD {size} @ {sell_price:.4f} | ${proceeds:.2f} proceeds | ${profit:+.2f} profit")
        tg(f"💰 <b>BTC $80k NO SOLD</b>\n{size} shares @ {sell_price:.4f}\nProceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}")
    else:
        print(f"\nFailed: {receipt.get('errorMsg','')} | Full: {receipt}")
except Exception as e:
    print(f"\nError: {e}")
