"""
Sell US Forces Enter Iran by December 31 NO position
228.888 shares at ~29-30¢ market
"""
import os, sys, math, time, requests
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

# US Forces Enter Iran by Dec 31 — NO token
NO_TOKEN = '999431386086915652803375764895435840752699372365775899613514207437695781135'

# Check current balance of this token
try:
    from py_clob_client.clob_types import BalanceAllowanceParams as BAP, AssetType as AT
    bal_info = client.get_balance_allowance(params=BAP(
        asset_type=AT.CONDITIONAL, token_id=NO_TOKEN, signature_type=2))
    raw_bal = int(bal_info.get('balance', 0))
    shares = raw_bal / 1e6
    print(f"Confirmed NO token balance: {shares:.4f} shares")
except Exception as e:
    print(f"Balance check: {e}")
    shares = 228.888  # use known value

# Get order book to find best sell price
tick = client.get_tick_size(NO_TOKEN)
neg_risk = client.get_neg_risk(NO_TOKEN)
tick_f = float(tick)
tick_dec = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0

ob = client.get_order_book(NO_TOKEN)
bids = ob.bids if hasattr(ob, 'bids') and ob.bids else []

# Best bid on NO side = what we receive when selling
if bids:
    best_bid = float(bids[0].price)
    print(f"Best bid on NO: {best_bid:.4f}")
else:
    best_bid = 0.29

# Round to tick
sell_price = round(round(best_bid / tick_f) * tick_f, tick_dec)
sell_price = max(0.01, min(0.99, sell_price))

# Use floor to avoid "not enough balance" errors
sell_shares = math.floor(shares * 100) / 100
print(f"Selling {sell_shares} NO shares @ {sell_price:.4f}")
print(f"Expected proceeds: ${sell_shares * sell_price:.2f}")

# Approve conditional token first
try:
    client.update_balance_allowance(params=BAP(
        asset_type=AT.CONDITIONAL, token_id=NO_TOKEN, signature_type=2))
    print("Conditional token approved")
except Exception as e:
    print(f"Approval: {e}")

# Place sell order (GTC limit at best bid)
args    = OrderArgs(token_id=NO_TOKEN, price=sell_price, size=sell_shares, side=SELL)
options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
signed  = client.create_order(args, options)
receipt = client.post_order(signed, OrderType.GTC)

print(f"Receipt: {receipt}")

if receipt.get('success') or receipt.get('orderID'):
    proceeds = sell_shares * sell_price
    oid = receipt.get('orderID','N/A')
    print(f"✅ SELL ORDER PLACED — {oid[:20]}...")
    print(f"Estimated proceeds: ${proceeds:.2f}")
    tg(f"<b>🔴 SELL: US Forces Dec 31 NO</b>\n{sell_shares:.1f} shares @ {sell_price:.3f}\nProceeds: ~${proceeds:.2f}\nReason: Marines deploying, NO side deteriorating\nOrder ID: {oid[:20]}...")
else:
    err = receipt.get('errorMsg', str(receipt))
    print(f"❌ Error: {err}")
    tg(f"<b>SELL FAILED: Dec 31 NO</b>\n{err[:100]}")
