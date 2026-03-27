import os, sys, math, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, OrderType, PartialCreateOrderOptions,
    BalanceAllowanceParams, AssetType
)
from py_clob_client.order_builder.constants import SELL

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER      = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN    = os.environ.get('TELEGRAM_TOKEN','')
TG_CHAT     = os.environ.get('TELEGRAM_CHAT_ID','')

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
        except: pass

client = ClobClient('https://clob.polymarket.com', key=PRIVATE_KEY,
                    chain_id=137, signature_type=2, funder=FUNDER)
client.set_api_creds(client.create_or_derive_api_creds())

NO_TOKEN = '92274168594309301674860132535303917340223623966677593281435938105131107664927'

try:
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL, token_id=NO_TOKEN, signature_type=2))
    shares = int(bal.get('balance', 0)) / 1e6
    print(f"Balance: {shares:.4f} shares")
except Exception as e:
    print(f"Balance err: {e}")
    shares = 117.6

if shares < 0.01:
    print("Nothing to sell")
else:
    tick = client.get_tick_size(NO_TOKEN)
    neg_risk = client.get_neg_risk(NO_TOKEN)
    tick_f = float(tick)
    tick_dec = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0

    ob = client.get_order_book(NO_TOKEN)
    bids = ob.bids if hasattr(ob, 'bids') and ob.bids else []
    
    if not bids:
        print("No bids")
    else:
        best_bid = float(bids[0].price)
        sell_price = round(round(best_bid / tick_f) * tick_f, tick_dec)
        sell_price = max(0.01, min(0.99, sell_price))
        sell_shares = math.floor(shares * 100) / 100
        proceeds = sell_shares * sell_price
        print(f"Best bid: {best_bid:.4f} | Selling {sell_shares} @ {sell_price:.4f} = ${proceeds:.2f}")

        try:
            client.update_balance_allowance(params=BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=NO_TOKEN, signature_type=2))
        except: pass

        args    = OrderArgs(token_id=NO_TOKEN, price=sell_price, size=sell_shares, side=SELL)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed  = client.create_order(args, options)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get('success') or receipt.get('orderID'):
            print(f"SOLD {sell_shares:.1f} @ {sell_price:.3f} = ${proceeds:.2f}")
            tg(f"<b>SELL: Crude Oil $100 NO</b>\n{sell_shares:.1f} shares @ {sell_price:.3f}\nProceeds: ~${proceeds:.2f}\nReason: WTI at $99+ — too risky")
        else:
            print(f"FAIL: {receipt.get('errorMsg', str(receipt))}")
