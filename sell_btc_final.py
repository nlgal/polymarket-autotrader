"""
sell_btc_final.py - Sell BTC $80k NO using neg_risk aware approach
The token is a neg-risk conditional token requiring NEG_RISK_ADAPTER approval.
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
SHARES = 337.24

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
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print("Creds OK")
except Exception as e:
    print(f"Creds: {e}")

# Get mid
r = requests.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN_ID}", timeout=8)
mid = float(r.json().get("mid", 0.895)) if r.status_code == 200 else 0.895
pnl = (mid - ENTRY) * SHARES
print(f"\nBTC $80k NO: mid={mid:.4f} | gain=${pnl:+.2f} ({pnl/ENTRY/SHARES*100:+.1f}%)")

# Check neg_risk
neg_risk = client.get_neg_risk(TOKEN_ID)
tick = client.get_tick_size(TOKEN_ID)
print(f"neg_risk={neg_risk} tick={tick}")

# Approve ALL allowance types
print("\nApproving allowances...")
for at, tid in [(AssetType.COLLATERAL, None), (AssetType.CONDITIONAL, TOKEN_ID)]:
    try:
        kw = {"asset_type": at, "signature_type": 2}
        if tid: kw["token_id"] = tid
        res = client.update_balance_allowance(params=BalanceAllowanceParams(**kw))
        print(f"  {at}: OK - {str(res)[:60]}")
    except Exception as e:
        print(f"  {at}: {e}")

# Check current balance/allowance
try:
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL, token_id=TOKEN_ID, signature_type=2))
    print(f"\nConditional balance: {bal}")
except Exception as e:
    print(f"Balance check: {e}")

# Place the sell
print("\nPlacing sell order...")
tick_f = float(tick)
tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
sell_price = round(round(mid / tick_f) * tick_f, tick_dec)
sell_price = max(0.01, min(0.99, sell_price))
shares = round(SHARES, 2)
print(f"  {shares} shares @ {sell_price:.4f} (neg_risk={neg_risk})")

args = OrderArgs(token_id=TOKEN_ID, price=sell_price, size=shares, side=SELL)
opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
try:
    signed = client.create_order(args, opts)
    receipt = client.post_order(signed, OrderType.GTC)
    if receipt.get("success"):
        proceeds = sell_price * shares
        profit = proceeds - ENTRY * shares
        print(f"\n✓ SOLD! Proceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}")
        tg(f"💰 <b>BTC $80k NO SOLD</b>\n{shares} @ {sell_price:.4f}\nProceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}")
    else:
        err = receipt.get("errorMsg","")
        print(f"\n✗ Failed: {err}")
        # If still allowance error, print the full response
        print(f"Full receipt: {receipt}")
except Exception as e:
    print(f"\n✗ Error: {e}")
    import traceback; traceback.print_exc()
