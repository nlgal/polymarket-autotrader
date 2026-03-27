"""Sell BTC $65k dip NO position — Lesson 11: BTC was 1.8% above trigger when bought."""
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

# BTC $65k dip NO token (outcomeIndex=1 = NO side)
NO_TOKEN = '64087619211543545431479218048939484178441767712621033463416084593776314629222'

# Check actual balance
try:
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL, token_id=NO_TOKEN, signature_type=2))
    raw = int(bal.get('balance', 0))
    shares = raw / 1e6
    print(f"NO token balance: {shares:.4f} shares")
except Exception as e:
    print(f"Balance error: {e}")
    shares = 8.4  # fallback: ~8 shares bought for $2.66

if shares < 0.01:
    print("No balance to sell — already sold or position too small")
    exit()

# Get order book
tick = client.get_tick_size(NO_TOKEN)
neg_risk = client.get_neg_risk(NO_TOKEN)
tick_f = float(tick)
tick_dec = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0

ob = client.get_order_book(NO_TOKEN)
bids = ob.bids if hasattr(ob, 'bids') and ob.bids else []

if not bids:
    print("No bids — market may be illiquid. Trying at 0.01...")
    best_bid = 0.01
else:
    best_bid = float(bids[0].price)

print(f"Best bid: {best_bid:.4f}")

sell_price = round(round(best_bid / tick_f) * tick_f, tick_dec)
sell_price = max(0.01, min(0.99, sell_price))
sell_shares = math.floor(shares * 100) / 100
proceeds = sell_shares * sell_price

print(f"Selling {sell_shares} NO shares @ {sell_price:.4f} = ${proceeds:.2f}")

# Approve
try:
    client.update_balance_allowance(params=BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL, token_id=NO_TOKEN, signature_type=2))
    print("Token approved")
except Exception as e:
    print(f"Approval: {e}")

# Place sell
args    = OrderArgs(token_id=NO_TOKEN, price=sell_price, size=sell_shares, side=SELL)
options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
signed  = client.create_order(args, options)
receipt = client.post_order(signed, OrderType.GTC)

print(f"Receipt: {receipt}")

if receipt.get('success') or receipt.get('orderID'):
    oid = receipt.get('orderID','N/A')
    print(f"✅ SOLD {sell_shares:.1f} NO @ {sell_price:.3f} = ${proceeds:.2f}")
    tg(f"<b>🔴 SELL: BTC $65k dip NO</b>\n{sell_shares:.1f} shares @ {sell_price:.3f}\nProceeds: ~${proceeds:.2f}\nReason: Lesson 11 — BTC was within 2% of trigger\nOrder: {oid[:20]}...")
else:
    err = receipt.get('errorMsg', str(receipt))
    print(f"❌ {err}")
    tg(f"<b>SELL FAILED: BTC $65k NO</b>\n{err[:100]}")
