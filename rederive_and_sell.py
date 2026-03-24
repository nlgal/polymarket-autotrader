
import os, sys
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY","").strip()
FUNDER = os.environ.get("POLYMARKET_FUNDER_ADDRESS","").strip()
SIG_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE","2"))
TOKEN_ID = "96839284769036407740491691016901048240322264125970194871307313464800669089139"
ENTRY = 0.5656
SHARES = 337.24

HOST = "https://clob.polymarket.com"

# Step 1: Create client with NO existing creds to force fresh derivation
client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=137, signature_type=SIG_TYPE, funder=FUNDER)

# Step 2: Derive fresh API creds
print("Deriving fresh CLOB API credentials...")
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)
print(f"New API key: {creds.api_key[:20]}...")

# Step 3: Save new creds to .env
env_path = '/opt/polymarket-agent/.env'
with open(env_path) as f:
    env = f.read()

for key, val in [("CLOB_API_KEY", creds.api_key),
                 ("CLOB_API_SECRET", creds.api_secret),
                 ("CLOB_API_PASSPHRASE", creds.api_passphrase)]:
    if key + "=" in env:
        import re
        env = re.sub(f"^{key}=.*$", f"{key}={val}", env, flags=re.MULTILINE)
    else:
        env += f"\n{key}={val}"

with open(env_path, "w") as f:
    f.write(env)
print("Saved creds to .env")

# Step 4: Now approve conditional tokens and sell BTC
import requests as req

# Get current mid
r = req.get(f"https://clob.polymarket.com/midpoint?token_id={TOKEN_ID}", timeout=8)
mid = float(r.json().get("mid", 0.895)) if r.status_code == 200 else 0.895
print(f"BTC 80k NO mid: {mid:.4f}")

# Approve conditional token
print("Approving conditional token...")
try:
    result = client.update_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL,
                                      token_id=TOKEN_ID, signature_type=2))
    print(f"Conditional approval result: {result}")
except Exception as e:
    print(f"Conditional approval error: {e}")

# Place sell order
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import SELL

try:
    tick = client.get_tick_size(TOKEN_ID)
    neg_risk = client.get_neg_risk(TOKEN_ID)
    tick_f = float(tick)
    tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
    sell_price = round(round(mid / tick_f) * tick_f, tick_dec)
    sell_price = max(0.01, min(0.99, sell_price))
    shares = round(SHARES, 2)
    
    print(f"Selling {shares} @ {sell_price:.4f}...")
    args = OrderArgs(token_id=TOKEN_ID, price=sell_price, size=shares, side=SELL)
    opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
    signed = client.create_order(args, opts)
    receipt = client.post_order(signed, OrderType.GTC)
    
    if receipt.get("success"):
        proceeds = sell_price * shares
        profit = proceeds - ENTRY * shares
        print(f"SOLD {shares} @ {sell_price:.4f} | Proceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}")
        
        # Notify Telegram
        TG = os.environ.get("TELEGRAM_TOKEN","")
        CHAT = os.environ.get("TELEGRAM_CHAT_ID","")
        if TG and CHAT:
            req.post(f"https://api.telegram.org/bot{TG}/sendMessage",
                json={"chat_id":CHAT,"text":f"<b>BTC $80k NO SOLD</b>\nProceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}","parse_mode":"HTML"}, timeout=10)
    else:
        print(f"Sell failed: {receipt.get('errorMsg','')}")
except Exception as e:
    print(f"Sell error: {e}")
    import traceback; traceback.print_exc()

print("Done")
