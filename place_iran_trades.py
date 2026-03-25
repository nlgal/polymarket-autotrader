"""
place_iran_trades.py — 5-trade batch targeting high-conviction gaps
Trades:
1. Iranian regime fall by April 30 — BUY NO $150
2. US x Iran ceasefire by April 15 — BUY YES $100
3. Bitcoin dip to $65k in March — BUY NO $100
4. Crude Oil LOW $80 by March 31 — BUY NO $75
5. US forces enter Iran by March 31 — BUY NO $75
"""
import os, sys, math, time, json, requests
sys.path.insert(0, '/opt/polymarket-agent')
from dotenv import load_dotenv
load_dotenv('/opt/polymarket-agent/.env')

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, OrderType, PartialCreateOrderOptions,
    BalanceAllowanceParams, AssetType
)
from py_clob_client.order_builder.constants import BUY

PRIVATE_KEY = os.environ['POLYMARKET_PRIVATE_KEY']
FUNDER = os.environ['POLYMARKET_FUNDER_ADDRESS']
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN','')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID','')

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)
        except: pass

client = ClobClient('https://clob.polymarket.com', key=PRIVATE_KEY,
                    chain_id=137, signature_type=2, funder=FUNDER)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

# Get USDC cash
try:
    bal = client.get_balance_allowance(
        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2))
    usdc_cash = float(bal.get('balance', 0)) / 1e6
    print(f"USDC available: ${usdc_cash:.2f}")
except Exception as e:
    print(f"Balance check: {e}")
    usdc_cash = 500

# Check existing positions to avoid duplicates
existing = set()
try:
    r = requests.get(f'https://data-api.polymarket.com/positions?user={FUNDER}&limit=50', timeout=10)
    for p in r.json():
        cv = float(p.get('currentValue', 0))
        if cv > 5:
            existing.add(p.get('title','').lower()[:50])
except: pass

# [YES_token_id, NO_token_id, action, size_usdc, max_price, name]
TRADES = [
    {
        'name': 'Iranian regime fall Apr 30 — NO',
        'action': 'BUY_NO',
        'yes_token': '48764428286656921488851644351774667118009263342042758531252625616470924946480',
        'no_token':  '45752951190517118746418545365916139233368614665273368123939609626397431866529',
        'size_usdc': 150,
        'max_price': 0.95,
        'skip_if': 'will the iranian regime fall by april 30',
    },
    {
        'name': 'Ceasefire by April 15 — YES',
        'action': 'BUY_YES',
        'yes_token': '85191934649046129480174964255278880752271767733539167443243111973456166096127',
        'no_token':  '8442709013751543525223072638303914942960068246422295030411662679470140144155',
        'size_usdc': 100,
        'max_price': 0.45,
        'skip_if': 'us x iran ceasefire by april 15',
    },
    {
        'name': 'Bitcoin dip $65k in March — NO',
        'action': 'BUY_NO',
        'yes_token': '112493481455469093769281852159558847572704253342416714876781522096078968514094',
        'no_token':  '64087619211543545431479218048939484178441767712621033463416084593776314629222',
        'size_usdc': 100,
        'max_price': 0.89,
        'skip_if': 'will bitcoin dip to $65,000 in march',
    },
    {
        'name': 'Crude Oil LOW $80 Mar 31 — NO',
        'action': 'BUY_NO',
        'yes_token': '114929598274366971131336205799393924832857816779736104698632388348104809344836',
        'no_token':  '36145084663453267666801423060957500580695518567612602414112493781080641634240',
        'size_usdc': 75,
        'max_price': 0.86,
        'skip_if': 'will crude oil (cl) hit (low) $80 by end of march',
    },
    {
        'name': 'US forces enter Iran Mar 31 — NO',
        'action': 'BUY_NO',
        'yes_token': '42750054381142639205639663180818682570869285140532640407891991570656047928885',
        'no_token':  '81697486240392901899167649997008736380137911909662773455994395620863894931973',
        'size_usdc': 75,
        'max_price': 0.92,
        'skip_if': 'us forces enter iran by march 31',
    },
]

results = []
for trade in TRADES:
    name = trade['name']
    action = trade['action']
    token_id = trade['no_token'] if action == 'BUY_NO' else trade['yes_token']
    size_usdc = trade['size_usdc']
    max_price = trade['max_price']
    skip_key = trade.get('skip_if','')

    # Skip if already holding
    if any(skip_key in e for e in existing):
        print(f"SKIP (already holding): {name}")
        results.append({'name': name, 'status': 'SKIP', 'reason': 'already holding'})
        continue

    if usdc_cash < size_usdc * 0.5:
        print(f"SKIP (low cash ${usdc_cash:.0f}): {name}")
        results.append({'name': name, 'status': 'SKIP', 'reason': f'low cash ${usdc_cash:.0f}'})
        continue

    print(f"\n--- {name} ---")
    try:
        tick = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
        tick_f = float(tick)
        tick_dec = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0

        ob = client.get_order_book(token_id)
        asks = ob.asks if hasattr(ob, 'asks') and ob.asks else []
        if not asks:
            print(f"  No asks — skip")
            results.append({'name': name, 'status': 'SKIP', 'reason': 'no asks'})
            continue

        raw_price = float(asks[-1].price)
        price = round(round(raw_price / tick_f) * tick_f, tick_dec)
        price = max(0.01, min(0.99, price))
        print(f"  Ask: {raw_price:.4f} → {price:.4f} (max={max_price})")

        if price > max_price:
            print(f"  SKIP: price {price:.3f} > max {max_price:.3f}")
            results.append({'name': name, 'status': 'SKIP', 'reason': f'price {price:.3f}'})
            continue

        num_shares = round(size_usdc / price, 2)
        print(f"  Buying {num_shares} shares @ {price} = ${num_shares*price:.2f}")

        args = OrderArgs(token_id=token_id, price=price, size=num_shares, side=BUY)
        options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
        signed = client.create_order(args, options)
        receipt = client.post_order(signed, OrderType.GTC)

        if receipt.get('success') or receipt.get('orderID'):
            oid = receipt.get('orderID','N/A')
            print(f"  ✅ ORDER PLACED — {oid[:20]}...")
            usdc_cash -= size_usdc
            # Approve conditional token for selling
            try:
                client.update_balance_allowance(
                    params=BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id, signature_type=2))
            except: pass
            results.append({'name': name, 'status': 'OK', 'shares': num_shares,
                           'price': price, 'usdc': size_usdc})
        else:
            err = receipt.get('errorMsg', str(receipt))
            print(f"  ❌ Rejected: {err}")
            results.append({'name': name, 'status': 'FAIL', 'error': err[:80]})

        time.sleep(1.5)
    except Exception as e:
        import traceback
        print(f"  ERROR: {e}")
        traceback.print_exc()
        results.append({'name': name, 'status': 'ERROR', 'error': str(e)[:100]})

# Summary
print('\n' + '='*50)
ok = [r for r in results if r['status'] == 'OK']
total = sum(r.get('usdc',0) for r in ok)
print(f"Placed: {len(ok)}/{len(results)} | Deployed: ${total}")

msg_lines = ['<b>📊 5-Trade Batch Complete</b>\n']
for r in results:
    s = r['status']
    if s == 'OK':
        msg_lines.append(f"✅ {r['name']}\n   {r['shares']} @ {r['price']:.3f} (${r['usdc']})")
    elif s == 'SKIP':
        msg_lines.append(f"⏭️ {r['name']}: {r.get('reason','')}")
    else:
        msg_lines.append(f"❌ {r['name']}: {r.get('error','')[:60]}")
msg_lines.append(f'\n<b>Total: ${total}</b>')
tg('\n\n'.join(msg_lines))
