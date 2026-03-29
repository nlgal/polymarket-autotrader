
import os, sys, json, requests
sys.path.insert(0, "/opt/polymarket-agent")
from dotenv import load_dotenv
load_dotenv("/opt/polymarket-agent/.env")

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

PRIVATE_KEY = os.environ["POLYMARKET_PRIVATE_KEY"]
FUNDER      = os.environ["POLYMARKET_FUNDER_ADDRESS"]
TG_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT     = os.environ.get("TELEGRAM_CHAT_ID", "")

YES_TOKEN = "14601167560606426917718331855347642051223131748556197437271063122555636628254"
SIZE_USDC = 300

client = ClobClient(
    "https://clob.polymarket.com",
    key=PRIVATE_KEY, chain_id=POLYGON,
    signature_type=2, funder=FUNDER,
)
client.set_api_creds(client.create_or_derive_api_creds())

rb = requests.get(f"https://clob.polymarket.com/book?token_id={YES_TOKEN}", timeout=10)
asks = rb.json().get("asks", [])
if not asks:
    print("ERROR: No asks"); exit(1)

best_ask = float(asks[0]["price"])
tick     = client.get_tick_size(YES_TOKEN)
neg      = client.get_neg_risk(YES_TOKEN)
tick_f   = float(tick)
tick_dec = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
buy_price = round(round(best_ask / tick_f) * tick_f, tick_dec)
shares    = round(SIZE_USDC / buy_price, 2)

print(f"Buying {shares} Clippers YES @ {buy_price} = ${shares*buy_price:.2f}")

args   = OrderArgs(token_id=YES_TOKEN, price=buy_price, size=shares, side=BUY)
opts   = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg)
signed = client.create_order(args, opts)
result = client.post_order(signed, OrderType.GTC)
print(f"Result: {json.dumps(result)}")

ok = result.get("success") or result.get("orderID")
if ok and TG_TOKEN and TG_CHAT:
    msg = f"<b>🏀 Clippers YES placed</b>\n{shares} shares @ {buy_price} = ${shares*buy_price:.2f}\ndkxbt call: full port LAC vs tanking Bucks"
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
